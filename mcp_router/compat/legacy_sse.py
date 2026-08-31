from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass, field
from types import MappingProxyType

import httpx
from starlette.requests import Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from mcp_router.core.config_loader import Endpoint, LegacySSEBridgeConfig
from mcp_router.core.policy import CapabilityPolicy
from mcp_router.core.runtime import EndpointRuntime, RuntimeState
from mcp_router.core.sse import iter_sse_events, render_sse_event, transform_sse_event

_MAX_TOMBSTONES = 4096
_MAX_UPSTREAM_ERROR_DETAIL = 512


@dataclass
class BridgeMetrics:
    sessions_opened_total: int = 0
    posts_total: int = 0
    downstream_disconnects_total: int = 0
    expired_sessions_total: int = 0
    session_limit_rejections_total: int = 0
    upstream_failures_total: int = 0
    backpressure_failures_total: int = 0

    def as_dict(self, *, active_sessions: int) -> dict[str, int]:
        return {
            "active_sessions": active_sessions,
            "sessions_opened_total": self.sessions_opened_total,
            "posts_total": self.posts_total,
            "downstream_disconnects_total": self.downstream_disconnects_total,
            "expired_sessions_total": self.expired_sessions_total,
            "session_limit_rejections_total": self.session_limit_rejections_total,
            "upstream_failures_total": self.upstream_failures_total,
            "backpressure_failures_total": self.backpressure_failures_total,
        }


@dataclass(frozen=True)
class ClosedSession:
    path_prefix: str
    status_code: int
    message: str


class _TrackedUpstream:
    def __init__(
        self,
        context: AbstractAsyncContextManager[httpx.Response],
    ) -> None:
        self._context = context
        self._closed = False
        self._lock = asyncio.Lock()

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._context.__aexit__(None, None, None)


@dataclass(eq=False)
class BridgeSession:
    session_id: str
    path_prefix: str
    config: LegacySSEBridgeConfig
    runtime: EndpointRuntime
    queue: asyncio.Queue[bytes]
    created_at: float
    expires_at: float
    remote_session_id: str | None = None
    setup_tasks: set[asyncio.Task[object]] = field(default_factory=set, repr=False)
    response_tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)
    upstreams: set[_TrackedUpstream] = field(default_factory=set, repr=False)
    expiry_task: asyncio.Task[None] | None = field(default=None, repr=False)
    closed: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    activity: asyncio.Event = field(default_factory=asyncio.Event, repr=False)
    cleanup_lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    terminal_event: bytes | None = None

    def touch(self) -> None:
        self.expires_at = time.monotonic() + self.config.session_ttl
        self.activity.set()


class LegacySSEBridge:
    """Explicit, bounded compatibility adapter for legacy SSE clients."""

    def __init__(
        self,
        *,
        client_provider: Callable[[], Awaitable[httpx.AsyncClient]],
        response_redactor: Callable[[str], str],
        log_redactor: Callable[[str], str],
    ) -> None:
        self._client_provider = client_provider
        self._response_redactor = response_redactor
        self._log_redactor = log_redactor
        self._sessions: dict[str, BridgeSession] = {}
        self._owned_sessions: set[BridgeSession] = set()
        self._closed_sessions: dict[str, ClosedSession] = {}
        self._metrics: dict[str, BridgeMetrics] = {}

    @property
    def sessions(self) -> Mapping[str, BridgeSession]:
        return MappingProxyType(self._sessions)

    def metrics_snapshot(self, path_prefix: str) -> dict[str, int]:
        metrics = self._metrics.get(path_prefix, BridgeMetrics())
        active = sum(
            1
            for session in self._sessions.values()
            if session.path_prefix == path_prefix and not session.closed.is_set()
        )
        return metrics.as_dict(active_sessions=active)

    def open_session(
        self,
        *,
        endpoint: Endpoint,
        runtime: EndpointRuntime,
    ) -> Response:
        config = endpoint.legacy_sse_bridge
        if config is None:
            return JSONResponse(
                {"error": "Legacy SSE compatibility is disabled for this endpoint"},
                status_code=400,
            )

        active_count = sum(
            1
            for session in self._sessions.values()
            if session.path_prefix == endpoint.path and not session.closed.is_set()
        )
        if active_count >= config.max_sessions:
            self._metric(endpoint.path).session_limit_rejections_total += 1
            return JSONResponse(
                {"error": "Legacy SSE session limit reached for this endpoint"},
                status_code=429,
            )

        from uuid import uuid4

        session_id = uuid4().hex
        now = time.monotonic()
        session = BridgeSession(
            session_id=session_id,
            path_prefix=endpoint.path,
            config=config,
            runtime=runtime,
            queue=asyncio.Queue(maxsize=config.queue_capacity),
            created_at=now,
            expires_at=now + config.session_ttl,
        )
        self._sessions[session_id] = session
        self._owned_sessions.add(session)
        runtime.legacy_session_ids.add(session_id)
        self._metric(endpoint.path).sessions_opened_total += 1

        expiry_task = asyncio.create_task(
            self._expiry_loop(session),
            name=f"mcp-mux:{endpoint.path}:legacy-expiry",
        )
        session.expiry_task = expiry_task
        runtime.track_legacy_task(expiry_task)

        return StreamingResponse(
            self._stream_session(session),
            media_type="text/event-stream",
            headers={
                "Cache-Control": "no-cache",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    async def handle_post(
        self,
        *,
        request: Request,
        endpoint: Endpoint,
        runtime: EndpointRuntime,
        path_prefix: str,
        target_url: str,
        forward_headers: dict[str, str],
        request_body: bytes,
        session_id: str,
        policy: CapabilityPolicy,
        principal: str,
        on_complete: Callable[[], Awaitable[None]] | None = None,
    ) -> Response:
        async def finish(response: Response) -> Response:
            if on_complete is not None:
                await on_complete()
            return response

        session = self._sessions.get(session_id)
        if session is None:
            closed = self._closed_sessions.get(session_id)
            if closed is not None:
                if closed.path_prefix != path_prefix:
                    return await finish(
                        JSONResponse(
                            {"error": "Session belongs to a different endpoint"},
                            status_code=409,
                        )
                    )
                return await finish(
                    JSONResponse({"error": closed.message}, status_code=closed.status_code)
                )
            return await finish(JSONResponse({"error": "Session not found"}, status_code=404))

        if session.path_prefix != path_prefix or session.runtime is not runtime:
            return await finish(
                JSONResponse(
                    {"error": "Session belongs to a different endpoint"},
                    status_code=409,
                )
            )
        if time.monotonic() >= session.expires_at:
            self._metric(path_prefix).expired_sessions_total += 1
            await self._mark_terminal(
                session,
                status_code=410,
                code="session_expired",
                message="Legacy SSE session expired",
            )
            await self._close_session_object(session)
            return await finish(
                JSONResponse({"error": "Legacy SSE session expired"}, status_code=410)
            )

        session.touch()
        self._metric(path_prefix).posts_total += 1
        params = {
            key: value
            for key, value in request.query_params.items()
            if key != "session_id"
        }
        if session.remote_session_id:
            forward_headers["Mcp-Session-Id"] = session.remote_session_id
        else:
            forward_headers.pop("Mcp-Session-Id", None)
            forward_headers.pop("mcp-session-id", None)
        forward_headers["accept"] = "application/json, text/event-stream"

        async def establish_upstream() -> tuple[
            AbstractAsyncContextManager[httpx.Response],
            httpx.Response,
        ]:
            client = await self._client_provider()
            stream_context = client.stream(
                method="POST",
                url=target_url,
                headers=forward_headers,
                params=params,
                content=request_body,
                timeout=httpx.Timeout(endpoint.upstream_timeout, read=None),
            )
            response = await asyncio.wait_for(
                stream_context.__aenter__(),
                timeout=endpoint.upstream_timeout,
            )
            return stream_context, response

        setup_task = asyncio.create_task(
            establish_upstream(),
            name=f"mcp-mux:{path_prefix}:legacy-setup",
        )
        session.setup_tasks.add(setup_task)
        setup_task.add_done_callback(session.setup_tasks.discard)
        runtime.track_legacy_task(setup_task)
        try:
            stream_context, response = await setup_task
        except (
            TimeoutError,
            httpx.HTTPError,
            OSError,
            RuntimeError,
            ValueError,
        ) as exc:
            self._metric(path_prefix).upstream_failures_total += 1
            return await finish(
                JSONResponse(
                    {
                        "error": "Legacy bridge upstream request failed",
                        "detail": self._log_redactor(str(exc))[:_MAX_UPSTREAM_ERROR_DETAIL],
                    },
                    status_code=502,
                )
            )

        tracked = _TrackedUpstream(stream_context)

        def post_setup_rejection() -> Response | None:
            if session.closed.is_set() or self._sessions.get(session_id) is not session:
                closed = self._closed_sessions.get(session_id)
                if closed is not None and closed.path_prefix == path_prefix:
                    return JSONResponse(
                        {"error": closed.message},
                        status_code=closed.status_code,
                    )
                return JSONResponse(
                    {"error": "Legacy SSE session closed during upstream setup"},
                    status_code=410,
                )
            if runtime.state is RuntimeState.DRAINING:
                return JSONResponse(
                    {"error": "Endpoint runtime is draining"},
                    status_code=503,
                )
            return None

        if not 200 <= response.status_code < 300:
            async with session.cleanup_lock:
                rejection = post_setup_rejection()
                if rejection is None:
                    session.upstreams.add(tracked)
            if rejection is not None:
                await tracked.close()
                return await finish(rejection)
            self._metric(path_prefix).upstream_failures_total += 1
            detail = ""
            try:
                body = await asyncio.wait_for(
                    response.aread(),
                    timeout=endpoint.upstream_timeout,
                )
                detail = self._response_redactor(
                    body.decode("utf-8", errors="replace")
                )[:_MAX_UPSTREAM_ERROR_DETAIL]
            except (
                TimeoutError,
                httpx.HTTPError,
                OSError,
                RuntimeError,
                UnicodeError,
                ValueError,
            ):
                detail = ""
            finally:
                await tracked.close()
                session.upstreams.discard(tracked)
            payload: dict[str, object] = {
                "error": "Legacy bridge upstream rejected request",
                "upstream_status": response.status_code,
            }
            if detail:
                payload["detail"] = detail
            return await finish(JSONResponse(payload, status_code=502))

        response_started = asyncio.Event()
        response_entered = False

        async def process_response() -> None:
            nonlocal response_entered
            try:
                # Lease ownership is not transferred merely by creating this task.
                # Signal only after execution has entered the cancellation-safe
                # try/finally that owns upstream cleanup and on_complete().
                response_entered = True
                response_started.set()
                content_type = response.headers.get("content-type", "").casefold()
                if "application/json" in content_type:
                    body_bytes = await asyncio.wait_for(
                        response.aread(),
                        timeout=endpoint.upstream_timeout,
                    )
                    body_str = body_bytes.decode("utf-8")
                    json.loads(body_str)
                    body_str = self._response_redactor(body_str)
                    projected_body, _ = policy.project_json_text(
                        body_str,
                        principal=principal,
                        endpoint=path_prefix,
                    )
                    await self._enqueue_event(
                        session,
                        self._message_event(projected_body),
                    )
                elif "event-stream" in content_type:
                    async for event in iter_sse_events(response.aiter_lines()):
                        def transform(data: str) -> str:
                            redacted = self._response_redactor(data)
                            projected, _ = policy.project_json_text(
                                redacted,
                                principal=principal,
                                endpoint=path_prefix,
                            )
                            return projected

                        rendered = render_sse_event(
                            transform_sse_event(event, transform)
                        )
                        if not await self._enqueue_event(session, rendered):
                            return
                else:
                    iterator = response.aiter_lines().__aiter__()
                    while True:
                        try:
                            line = await asyncio.wait_for(
                                anext(iterator),
                                timeout=endpoint.upstream_timeout,
                            )
                        except StopAsyncIteration:
                            break
                        redacted = self._response_redactor(line)
                        if not await self._enqueue_event(
                            session,
                            self._message_event(redacted),
                        ):
                            return
            except (
                TimeoutError,
                httpx.HTTPError,
                OSError,
                RuntimeError,
                UnicodeError,
                ValueError,
            ) as exc:
                self._metric(path_prefix).upstream_failures_total += 1
                await self._mark_terminal(
                    session,
                    status_code=502,
                    code="upstream_response_failed",
                    message=(
                        "Legacy bridge upstream response failed: "
                        f"{self._log_redactor(str(exc))[:_MAX_UPSTREAM_ERROR_DETAIL]}"
                    ),
                )
            finally:
                try:
                    await tracked.close()
                finally:
                    session.upstreams.discard(tracked)
                    if on_complete is not None:
                        await on_complete()

        response_task: asyncio.Task[None] | None = None
        async with session.cleanup_lock:
            rejection = post_setup_rejection()
            if rejection is None:
                session.upstreams.add(tracked)
                remote_session_id = response.headers.get("mcp-session-id")
                if remote_session_id:
                    session.remote_session_id = remote_session_id

                def response_done(task: asyncio.Task[None]) -> None:
                    session.response_tasks.discard(task)
                    # A task cancelled before its first execution never reaches
                    # process_response()'s try/finally. Wake the request-side
                    # handoff so it can retain cleanup/lease ownership.
                    response_started.set()

                response_task = asyncio.create_task(
                    process_response(),
                    name=f"mcp-mux:{path_prefix}:legacy-response",
                )
                session.response_tasks.add(response_task)
                response_task.add_done_callback(response_done)
                runtime.track_legacy_task(response_task)
        if rejection is not None:
            await tracked.close()
            return await finish(rejection)

        assert response_task is not None
        try:
            await response_started.wait()
        except BaseException:
            # The request still owns the leases until the response coroutine has
            # entered its protected lifetime. Request cancellation during this
            # handshake must therefore tear down any published response work.
            if not response_task.done():
                response_task.cancel()
            await asyncio.gather(response_task, return_exceptions=True)
            try:
                await tracked.close()
            finally:
                session.upstreams.discard(tracked)
            raise

        if not response_entered:
            # Cancellation won after publication but before the response coroutine
            # could enter its try/finally. No ownership transfer occurred: close the
            # entered upstream and release request-owned leases here.
            try:
                await tracked.close()
            finally:
                session.upstreams.discard(tracked)
            async with session.cleanup_lock:
                rejection = post_setup_rejection()
            if rejection is not None:
                return await finish(rejection)
            self._metric(path_prefix).upstream_failures_total += 1
            return await finish(
                JSONResponse(
                    {"error": "Legacy bridge response task ended before startup"},
                    status_code=502,
                )
            )

        return Response("Accepted", status_code=202)

    async def close_endpoint(self, path_prefix: str) -> None:
        sessions = [
            session
            for session in self._owned_sessions
            if session.path_prefix == path_prefix
        ]
        for session in sessions:
            await self._close_session_object(session)

    async def close_all(self) -> None:
        for session in list(self._owned_sessions):
            await self._close_session_object(session)

    async def _expiry_loop(self, session: BridgeSession) -> None:
        while not session.closed.is_set():
            delay = max(0.0, session.expires_at - time.monotonic())
            sleep_task = asyncio.create_task(asyncio.sleep(delay))
            activity_task = asyncio.create_task(session.activity.wait())
            closed_task = asyncio.create_task(session.closed.wait())
            done, pending = await asyncio.wait(
                {sleep_task, activity_task, closed_task},
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                task.cancel()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
            if closed_task in done and session.closed.is_set():
                return
            if activity_task in done and session.activity.is_set():
                session.activity.clear()
                continue
            if time.monotonic() >= session.expires_at:
                self._metric(session.path_prefix).expired_sessions_total += 1
                await self._mark_terminal(
                    session,
                    status_code=410,
                    code="session_expired",
                    message="Legacy SSE session expired",
                )
                await self._close_session_object(
                    session,
                    exclude_task=asyncio.current_task(),
                )
                return

    async def _stream_session(self, session: BridgeSession) -> AsyncIterator[bytes]:
        disconnected = False
        try:
            endpoint_uri = f"/{session.path_prefix}?session_id={session.session_id}"
            yield f"event: endpoint\ndata: {endpoint_uri}\n\n".encode()
            while True:
                queue_task = asyncio.create_task(session.queue.get())
                closed_task = asyncio.create_task(session.closed.wait())
                done, pending = await asyncio.wait(
                    {queue_task, closed_task},
                    return_when=asyncio.FIRST_COMPLETED,
                )
                for task in pending:
                    task.cancel()
                if pending:
                    await asyncio.gather(*pending, return_exceptions=True)

                if queue_task in done:
                    event = queue_task.result()
                    session.touch()
                    yield event
                    continue
                if session.closed.is_set():
                    if session.terminal_event is not None:
                        yield session.terminal_event
                    return
        except (GeneratorExit, asyncio.CancelledError):
            disconnected = True
            raise
        finally:
            if not session.closed.is_set():
                disconnected = True
            if disconnected:
                self._metric(session.path_prefix).downstream_disconnects_total += 1
            await self._close_session_object(session)

    async def _enqueue_event(self, session: BridgeSession, event: bytes) -> bool:
        if session.closed.is_set():
            return False
        try:
            await asyncio.wait_for(
                session.queue.put(event),
                timeout=session.config.backpressure_timeout,
            )
        except TimeoutError:
            self._metric(session.path_prefix).backpressure_failures_total += 1
            await self._mark_terminal(
                session,
                status_code=503,
                code="backpressure_exceeded",
                message="Legacy SSE client is not consuming bridge responses",
            )
            return False
        session.touch()
        return True

    async def _mark_terminal(
        self,
        session: BridgeSession,
        *,
        status_code: int,
        code: str,
        message: str,
    ) -> None:
        if session.closed.is_set():
            return
        self._sessions.pop(session.session_id, None)
        session.runtime.legacy_session_ids.discard(session.session_id)
        self._remember_closed(
            session.session_id,
            ClosedSession(
                path_prefix=session.path_prefix,
                status_code=status_code,
                message=message,
            ),
        )
        self._drain_queue(session.queue)
        session.terminal_event = self._error_event(
            code=code,
            message=message,
            status_code=status_code,
        )
        session.closed.set()

        current = asyncio.current_task()
        tasks: list[asyncio.Task[object]] = [
            *session.setup_tasks,
            *session.response_tasks,
        ]
        if session.expiry_task is not None:
            tasks.append(session.expiry_task)
        for task in tasks:
            if task is not current and not task.done():
                task.cancel()

    async def _close_session_object(
        self,
        session: BridgeSession,
        *,
        exclude_task: asyncio.Task[object] | None = None,
    ) -> None:
        async with session.cleanup_lock:
            self._sessions.pop(session.session_id, None)
            session.runtime.legacy_session_ids.discard(session.session_id)
            session.closed.set()

            current = asyncio.current_task()
            excluded = {task for task in (exclude_task, current) if task is not None}
            tasks: list[asyncio.Task[object]] = [
                *session.setup_tasks,
                *session.response_tasks,
            ]
            if session.expiry_task is not None:
                tasks.append(session.expiry_task)
            owned_tasks = [
                task
                for task in tasks
                if task not in excluded
            ]
            for task in owned_tasks:
                if not task.done():
                    task.cancel()
            if owned_tasks:
                await asyncio.gather(*owned_tasks, return_exceptions=True)

            for upstream in list(session.upstreams):
                try:
                    await upstream.close()
                finally:
                    session.upstreams.discard(upstream)
            self._drain_queue(session.queue)
            self._owned_sessions.discard(session)

    def _metric(self, path_prefix: str) -> BridgeMetrics:
        return self._metrics.setdefault(path_prefix, BridgeMetrics())

    def _remember_closed(self, session_id: str, closed: ClosedSession) -> None:
        self._closed_sessions[session_id] = closed
        while len(self._closed_sessions) > _MAX_TOMBSTONES:
            oldest = next(iter(self._closed_sessions))
            self._closed_sessions.pop(oldest, None)

    @staticmethod
    def _drain_queue(queue: asyncio.Queue[bytes]) -> None:
        while True:
            try:
                queue.get_nowait()
            except asyncio.QueueEmpty:
                return

    @staticmethod
    def _message_event(data: str) -> bytes:
        lines = ["event: message"]
        lines.extend(f"data: {line}" for line in data.splitlines() or [""])
        return ("\n".join(lines) + "\n\n").encode()

    @staticmethod
    def _error_event(*, code: str, message: str, status_code: int) -> bytes:
        payload = json.dumps(
            {
                "error": {
                    "code": code,
                    "message": message,
                    "status": status_code,
                }
            },
            separators=(",", ":"),
        )
        return f"event: error\ndata: {payload}\n\n".encode()
