from __future__ import annotations

import asyncio
import logging
import os
import time
from collections.abc import AsyncIterator, Iterator, Mapping, MutableMapping
from contextlib import asynccontextmanager
from typing import TYPE_CHECKING
from urllib.parse import urlparse, urlunparse

import httpx
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from mcp_router.core.config_loader import (
    ConfigWatcher,
    Endpoint,
    ManagedEndpointConfig,
    RouterConfig,
    SecurityConfig,
)
from mcp_router.core.limits import LimitLease, RequestLimiter
from mcp_router.core.policy import CapabilityPolicy
from mcp_router.core.process_manager import ProcessManager
from mcp_router.core.protocol import (
    CAPABILITY_DENIED,
    INVALID_REQUEST,
    MODERN_PROTOCOL_VERSION,
    REQUEST_LIMITED,
    ParsedJSONRPCRequest,
    ProtocolEra,
    ProtocolRequestError,
    build_jsonrpc_error,
    extract_policy_request_names,
    parse_jsonrpc_request,
    validate_protocol_request,
)
from mcp_router.core.redaction import SecretRedactor
from mcp_router.core.runtime import (
    EndpointRuntime,
    RuntimeState,
    RuntimeUnavailableError,
    UpstreamLease,
)
from mcp_router.core.security import (
    GatewaySecurityMiddleware,
    build_upstream_headers,
    sanitize_response_headers,
)
from mcp_router.core.sse import iter_sse_events, render_sse_event, transform_sse_event

if TYPE_CHECKING:
    from mcp_router.compat.legacy_sse import LegacySSEBridge

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("mcp_router")

_HOP_BY_HOP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "keep-alive",
}
_DEFAULT_MAX_REQUEST_BODY_BYTES = 1_048_576
_HTTP_LIMITS = httpx.Limits(
    max_connections=100,
    max_keepalive_connections=20,
    keepalive_expiry=30.0,
)


def _upstream_stream_timeout(timeout: float) -> httpx.Timeout:
    """Bound request setup/write/pool operations without imposing an SSE read-idle timeout."""
    return httpx.Timeout(timeout, read=None)


def get_target_url(config_url: str, request_path: str, path_prefix: str) -> str:
    parsed_cfg = urlparse(config_url)
    prefix = f"/{path_prefix}"
    suffix = request_path.removeprefix(prefix)

    cfg_path = parsed_cfg.path.rstrip("/")
    joined_path = cfg_path + suffix
    if not joined_path.startswith("/"):
        joined_path = "/" + joined_path

    return urlunparse(
        (
            parsed_cfg.scheme,
            parsed_cfg.netloc,
            joined_path,
            parsed_cfg.params,
            "",
            parsed_cfg.fragment,
        )
    )


def filter_tools_response(
    line_or_body: str,
    allowed_tools: list[str] | None,
    denied_tools: list[str] | None,
) -> str:
    """Compatibility wrapper around the same policy source used for direct calls."""
    policy = CapabilityPolicy(
        allowed_tools=None if allowed_tools is None else frozenset(allowed_tools),
        denied_tools=None if denied_tools is None else frozenset(denied_tools),
    )
    projected, _ = policy.project_json_text(
        line_or_body,
        principal="compatibility",
        endpoint="compatibility",
    )
    return projected


def build_response_headers(
    headers: Mapping[str, str],
    exclude_headers: set[str],
    body_was_decoded: bool = False,
    body_was_transformed: bool = False,
) -> dict[str, str]:
    sanitized = sanitize_response_headers(
        headers,
        body_was_decoded=body_was_decoded,
        body_was_transformed=body_was_transformed,
    )
    return {
        key: value
        for key, value in sanitized.items()
        if key.casefold() not in exclude_headers
    }


def _jsonrpc_error_response(error: ProtocolRequestError) -> JSONResponse:
    return JSONResponse(
        build_jsonrpc_error(
            error.code,
            error.message,
            request_id=error.request_id,
            data=error.data,
        ),
        status_code=error.status_code,
    )


def _transport_error_response(
    status_code: int,
    message: str,
    *,
    request_id: str | int | None = None,
) -> JSONResponse:
    return JSONResponse(
        build_jsonrpc_error(INVALID_REQUEST, message, request_id=request_id),
        status_code=status_code,
    )


def _is_json_content_type(value: str | None) -> bool:
    if not value:
        return False
    media_type = value.split(";", 1)[0].strip().casefold()
    return media_type == "application/json"


def _rewrite_legacy_endpoint_data(data: str, path_prefix: str) -> str:
    if not data.startswith(("/", "http://", "https://")):
        return data
    parsed = urlparse(data)
    if parsed.netloc:
        new_path = f"/{path_prefix}{parsed.path}"
        if parsed.query:
            new_path += f"?{parsed.query}"
        return new_path
    return f"/{path_prefix}{data}"


def _endpoint_requires_runtime_reset(previous: Endpoint, candidate: Endpoint) -> bool:
    if previous.mode != candidate.mode:
        return True
    if (
        previous.url != candidate.url
        or previous.transport != candidate.transport
        or previous.legacy_sse_bridge != candidate.legacy_sse_bridge
    ):
        return True
    if isinstance(previous, ManagedEndpointConfig) and isinstance(candidate, ManagedEndpointConfig):
        return any(
            (
                previous.argv != candidate.argv,
                previous.env != candidate.env,
                previous.cwd != candidate.cwd,
                previous.readiness != candidate.readiness,
                previous.unsafe_shell_command != candidate.unsafe_shell_command,
            )
        )
    return False


class _EndpointConfigView(MutableMapping[str, Endpoint]):
    """Compatibility mapping backed by the authoritative endpoint runtime registry."""

    def __init__(self, router: MCPRouter) -> None:
        self._router = router

    def __getitem__(self, key: str) -> Endpoint:
        return self._router._runtimes[key].config

    def __setitem__(self, key: str, value: Endpoint) -> None:
        if key != value.path:
            raise ValueError("endpoint mapping key must match endpoint path")
        self._router._runtimes[key] = EndpointRuntime.from_config(value)

    def __delitem__(self, key: str) -> None:
        del self._router._runtimes[key]

    def __iter__(self) -> Iterator[str]:
        return iter(self._router._runtimes)

    def __len__(self) -> int:
        return len(self._router._runtimes)


class MCPRouter:
    def __init__(self, app: Starlette, config_path: str):
        self.app = app
        self.config_path = config_path
        self.process_manager = ProcessManager()
        self._runtimes: dict[str, EndpointRuntime] = {}
        self._config_view = _EndpointConfigView(self)
        self._reload_lock = asyncio.Lock()
        self.max_request_body_bytes = _DEFAULT_MAX_REQUEST_BODY_BYTES
        self._legacy_bridge: LegacySSEBridge | None = None
        self._accepting_work = True
        self._running = False
        self._checker_task: asyncio.Task[None] | None = None
        self._http_client: httpx.AsyncClient | None = None
        self.security_config = SecurityConfig()
        self._redactor = SecretRedactor()
        self._limiter = RequestLimiter()

        self.app.add_middleware(
            GatewaySecurityMiddleware,
            get_config=lambda: self.security_config,
        )
        self.app.add_route(
            "/summary",
            self.get_summary,
            methods=["GET"],
        )
        self.app.add_route(
            "/{path_prefix:str}",
            self.catch_all_proxy,
            methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        )
        self.app.add_route(
            "/{path_prefix:str}/{subpath:path}",
            self.catch_all_proxy,
            methods=["GET", "POST", "PUT", "DELETE", "OPTIONS"],
        )

    @property
    def _configs(self) -> MutableMapping[str, Endpoint]:
        return self._config_view

    @_configs.setter
    def _configs(self, configs: Mapping[str, Endpoint]) -> None:
        self._runtimes = {
            path: EndpointRuntime.from_config(endpoint)
            for path, endpoint in configs.items()
        }

    async def open_http_client(self) -> httpx.AsyncClient:
        if self._http_client is None or self._http_client.is_closed:
            self._http_client = httpx.AsyncClient(limits=_HTTP_LIMITS)
        return self._http_client

    async def close_http_client(self) -> None:
        client = self._http_client
        self._http_client = None
        if client is not None and not client.is_closed:
            await client.aclose()

    async def _get_http_client(self) -> httpx.AsyncClient:
        return await self.open_http_client()

    def _get_legacy_bridge(self) -> LegacySSEBridge:
        bridge = self._legacy_bridge
        if bridge is None:
            # The compatibility module is deliberately imported only when an
            # explicitly configured legacy client actually needs it. Canonical
            # modern Streamable HTTP therefore has no bridge import/state path.
            from mcp_router.compat.legacy_sse import LegacySSEBridge

            bridge = LegacySSEBridge(
                client_provider=self._get_http_client,
                response_redactor=lambda text: self._redactor.redact_known_secrets(text),
                log_redactor=lambda text: self._redactor.redact(text),
            )
            self._legacy_bridge = bridge
        return bridge

    async def apply_configuration(self, config: RouterConfig) -> None:
        """Drain runtime-affecting changes before atomically publishing a validated snapshot."""
        new_endpoints = {endpoint.path: endpoint for endpoint in config.endpoints}

        async with self._reload_lock:
            current_runtimes = self._runtimes
            retired_paths = {
                path
                for path, runtime in current_runtimes.items()
                if path not in new_endpoints
                or _endpoint_requires_runtime_reset(runtime.config, new_endpoints[path])
            }

            for path in sorted(retired_paths):
                runtime = current_runtimes[path]
                logger.info("Draining runtime-affecting configuration change for %s.", path)
                await self.process_manager.drain_and_stop(
                    runtime,
                    final_state=RuntimeState.DRAINING,
                )
                if self._legacy_bridge is not None:
                    await self._legacy_bridge.close_endpoint(path)

            next_runtimes: dict[str, EndpointRuntime] = {}
            retained_runtimes: list[EndpointRuntime] = []
            for path, endpoint in new_endpoints.items():
                current = current_runtimes.get(path)
                if current is not None and path not in retired_paths:
                    current.config = endpoint
                    next_runtimes[path] = current
                    retained_runtimes.append(current)
                else:
                    next_runtimes[path] = EndpointRuntime.from_config(endpoint)

            # No await occurs in this publication block: cooperative request handlers
            # observe either the complete old snapshot or the complete new snapshot.
            self._runtimes = next_runtimes
            self.max_request_body_bytes = config.max_request_body_bytes
            self.security_config = config.security
            self._redactor = SecretRedactor.from_router_config(config)
            self.process_manager.set_redactor(self._redactor.redact)

            # Restart tasks created here cannot run until this coroutine yields, so
            # the complete replacement snapshot is authoritative before recovery begins.
            # Reconciliation is idempotent and only schedules retained FAILED runtimes
            # whose newly published policy permits another supervisor-owned attempt.
            for runtime in retained_runtimes:
                self.process_manager.reconcile_restart_policy(runtime)

        logger.info("Applied config. Active paths: %s", list(self._runtimes))

    async def idle_timeout_checker(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(10)
                current_time = time.monotonic()
                for runtime in list(self._runtimes.values()):
                    endpoint = runtime.config
                    if not isinstance(endpoint, ManagedEndpointConfig):
                        continue
                    if (
                        runtime.state is not RuntimeState.RUNNING
                        or not self.process_manager.is_running(runtime)
                        or runtime.active_leases > 0
                        or current_time - runtime.last_completed_activity <= endpoint.timeout
                    ):
                        continue

                    async with runtime.lock:
                        if (
                            runtime.state is not RuntimeState.RUNNING
                            or runtime.active_leases > 0
                            or time.monotonic() - runtime.last_completed_activity <= endpoint.timeout
                        ):
                            continue
                        runtime.state = RuntimeState.DRAINING

                    await runtime.wait_for_leases()
                    logger.info(
                        "Inactivity timeout (%ss) exceeded for %s. Stopping process.",
                        endpoint.timeout,
                        runtime.path,
                    )
                    await self.process_manager.stop_managed_server(runtime)
            except asyncio.CancelledError:
                break
            except (OSError, RuntimeError, ValueError) as exc:
                logger.error("Error in idle timeout checker: %s", exc)

    async def get_summary(self, request: Request) -> JSONResponse:
        del request
        summary_list = [
            {
                "path": runtime.config.path,
                "mode": runtime.config.mode,
                "summary": runtime.config.summary,
                "runtime_state": runtime.state.value,
                "active_upstream_leases": runtime.active_leases,
                "last_exit_code": runtime.last_exit_code,
                "restart_attempts": runtime.restart_attempts,
            }
            for runtime in self._runtimes.values()
        ]
        return JSONResponse({"endpoints": summary_list})

    async def _read_post_body(
        self,
        request: Request,
    ) -> tuple[bytes, ParsedJSONRPCRequest, ProtocolEra] | JSONResponse:
        if not _is_json_content_type(request.headers.get("content-type")):
            return _transport_error_response(
                415,
                "MCP JSON-RPC POST requests require Content-Type: application/json",
            )

        content_length = request.headers.get("content-length")
        if content_length is not None:
            try:
                declared_length = int(content_length)
            except ValueError:
                return _transport_error_response(400, "Invalid Content-Length header")
            if declared_length < 0:
                return _transport_error_response(400, "Invalid Content-Length header")
            if declared_length > self.max_request_body_bytes:
                return _transport_error_response(
                    413,
                    f"Request body exceeds {self.max_request_body_bytes} byte limit",
                )

        if hasattr(request, "_receive"):
            chunks: list[bytes] = []
            body_size = 0
            async for chunk in request.stream():
                body_size += len(chunk)
                if body_size > self.max_request_body_bytes:
                    return _transport_error_response(
                        413,
                        f"Request body exceeds {self.max_request_body_bytes} byte limit",
                    )
                chunks.append(chunk)
            body = b"".join(chunks)
        else:
            body = await request.body()

        if len(body) > self.max_request_body_bytes:
            return _transport_error_response(
                413,
                f"Request body exceeds {self.max_request_body_bytes} byte limit",
            )

        try:
            parsed = parse_jsonrpc_request(body)
            era = validate_protocol_request(parsed, request.headers)
        except ProtocolRequestError as exc:
            return _jsonrpc_error_response(exc)
        return body, parsed, era

    async def _acquire_upstream_lease(
        self,
        runtime: EndpointRuntime,
    ) -> tuple[UpstreamLease | None, JSONResponse | None]:
        if not self._accepting_work and self._running:
            return None, JSONResponse({"error": "Gateway is shutting down"}, status_code=503)

        if runtime.managed:
            if runtime.state is RuntimeState.DRAINING:
                return None, JSONResponse({"error": "Managed endpoint is draining"}, status_code=503)
            if runtime.state is RuntimeState.FAILED:
                return None, JSONResponse({"error": "Managed endpoint is failed"}, status_code=503)
            if runtime.state is RuntimeState.RUNNING and not self.process_manager.is_running(runtime):
                return None, JSONResponse(
                    {"error": "Managed endpoint process is unavailable"},
                    status_code=503,
                )
            if runtime.state is not RuntimeState.RUNNING:
                logger.info("On-demand activation triggered for: %s", runtime.path)
                try:
                    await self.process_manager.start_managed_server(runtime)
                except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                    logger.error(
                        "Failed to start managed server %s: %s",
                        runtime.path,
                        self._redactor.redact(str(exc)),
                    )
                    return None, JSONResponse(
                        {"error": "Failed to start managed server"},
                        status_code=500,
                    )

        try:
            return await runtime.acquire_lease(), None
        except RuntimeUnavailableError:
            return None, JSONResponse({"error": "Endpoint runtime unavailable"}, status_code=503)

    def _forward_headers(self, request: Request, endpoint: Endpoint) -> dict[str, str]:
        headers = build_upstream_headers(request.headers, endpoint)
        identity_header = self.security_config.trusted_proxy_identity_header.casefold()
        headers = {
            key: value
            for key, value in headers.items()
            if key.casefold() != identity_header
        }
        if endpoint.transport == "streamable-http" and request.method in {"POST", "DELETE"}:
            headers["accept"] = "application/json, text/event-stream"
        return headers

    def _safe_response_headers(
        self,
        headers: Mapping[str, str],
        *,
        body_was_decoded: bool = False,
        body_was_transformed: bool = False,
    ) -> dict[str, str]:
        sanitized = build_response_headers(
            headers,
            _HOP_BY_HOP_REQUEST_HEADERS,
            body_was_decoded=body_was_decoded,
            body_was_transformed=body_was_transformed,
        )
        return {
            key: value
            for key, value in sanitized.items()
            if self._redactor.redact_known_secrets(value) == value
        }

    @staticmethod
    def _principal(request: Request) -> str:
        scope = getattr(request, "scope", {})
        if isinstance(scope, dict):
            principal = scope.get("mcp.principal")
            if isinstance(principal, str) and principal:
                return principal
        return "local"

    @staticmethod
    async def _release_leases(
        *leases: LimitLease | UpstreamLease | None,
    ) -> None:
        for lease in leases:
            if lease is not None:
                await lease.release()

    async def _finish_leased_response(
        self,
        response: Response,
        *leases: LimitLease | UpstreamLease | None,
    ) -> Response:
        active_leases = tuple(lease for lease in leases if lease is not None)
        if not active_leases:
            return response
        if not isinstance(response, StreamingResponse):
            await self._release_leases(*active_leases)
            return response

        original_iterator = response.body_iterator

        async def leased_iterator() -> AsyncIterator[bytes | memoryview | str]:
            try:
                async for chunk in original_iterator:
                    yield chunk
            finally:
                close_iterator = getattr(original_iterator, "aclose", None)
                try:
                    if close_iterator is not None:
                        await close_iterator()
                finally:
                    # Lease ownership must end even when the upstream iterator's
                    # own teardown fails. Preserve that close exception while making
                    # runtime/limiter release the unconditional secondary cleanup.
                    await self._release_leases(*active_leases)

        response.body_iterator = leased_iterator()
        return response

    async def sse_proxy_generator(
        self,
        target_url: str,
        headers: Mapping[str, str],
        params: dict[str, str],
        path_prefix: str,
        timeout: float,
        principal: str,
    ) -> AsyncIterator[bytes]:
        client = await self._get_http_client()
        stream_context = client.stream(
            "GET",
            target_url,
            headers=headers,
            params=params,
            timeout=_upstream_stream_timeout(timeout),
        )
        response: httpx.Response | None = None
        try:
            response = await asyncio.wait_for(stream_context.__aenter__(), timeout=timeout)
            async for event in iter_sse_events(response.aiter_lines()):
                def transform(data: str) -> str:
                    rewritten = _rewrite_legacy_endpoint_data(data, path_prefix)
                    redacted = self._redactor.redact_known_secrets(rewritten)
                    current_endpoint = self._configs.get(path_prefix)
                    if current_endpoint is None:
                        return redacted
                    projected, _ = CapabilityPolicy.from_endpoint(current_endpoint).project_json_text(
                        redacted,
                        principal=principal,
                        endpoint=path_prefix,
                    )
                    return projected

                yield render_sse_event(transform_sse_event(event, transform))
        finally:
            if response is not None:
                await stream_context.__aexit__(None, None, None)

    async def local_sse_generator(
        self,
        session_id: str,
        queue: asyncio.Queue[str],
        path_prefix: str,
    ) -> AsyncGenerator[bytes]:
        runtime = self._runtimes.get(path_prefix)
        client_post_uri = f"/{path_prefix}?session_id={session_id}"
        yield f"event: endpoint\ndata: {client_post_uri}\n\n".encode()
        try:
            while True:
                line = await queue.get()
                yield (line + "\n").encode("utf-8")
        finally:
            self.active_sessions.pop(session_id, None)
            if runtime is not None:
                runtime.legacy_session_ids.discard(session_id)

    async def _bridge_post(
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

        session = self.active_sessions.get(session_id)
        if session is None:
            return await finish(JSONResponse({"error": "Session not found"}, status_code=404))
        if session.path_prefix != path_prefix:
            return await finish(
                JSONResponse({"error": "Session belongs to a different endpoint"}, status_code=409)
            )

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

        try:
            client = await self._get_http_client()
            stream_context = client.stream(
                method="POST",
                url=target_url,
                headers=forward_headers,
                params=params,
                content=request_body,
                timeout=_upstream_stream_timeout(endpoint.upstream_timeout),
            )
            response = await asyncio.wait_for(
                stream_context.__aenter__(),
                timeout=endpoint.upstream_timeout,
            )
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            logger.error(
                "Failed to proxy bridge POST to streamable-http backend: %s",
                self._redactor.redact(str(exc)),
            )
            return await finish(
                JSONResponse({"error": "Failed to proxy request"}, status_code=502)
            )

        try:
            remote_session_id = response.headers.get("mcp-session-id")
            if remote_session_id:
                session.remote_session_id = remote_session_id
        except BaseException:
            await stream_context.__aexit__(None, None, None)
            raise

        async def process_response() -> None:
            try:
                content_type = response.headers.get("content-type", "").casefold()
                if "application/json" in content_type:
                    body_bytes = await asyncio.wait_for(
                        response.aread(),
                        timeout=endpoint.upstream_timeout,
                    )
                    body_str = self._redactor.redact_known_secrets(
                        body_bytes.decode("utf-8", errors="replace")
                    )
                    projected_body, _ = policy.project_json_text(
                        body_str,
                        principal=principal,
                        endpoint=path_prefix,
                    )
                    await session.queue.put("event: message")
                    await session.queue.put(f"data: {projected_body}")
                    await session.queue.put("")
                elif "text/event-stream" in content_type or "event-stream" in content_type:
                    async for event in iter_sse_events(response.aiter_lines()):
                        def transform(data: str) -> str:
                            redacted = self._redactor.redact_known_secrets(data)
                            projected, _ = policy.project_json_text(
                                redacted,
                                principal=principal,
                                endpoint=path_prefix,
                            )
                            return projected

                        transformed = transform_sse_event(event, transform)
                        for line in transformed:
                            await session.queue.put(line)
                        await session.queue.put("")
                else:
                    line_iterator = response.aiter_lines().__aiter__()
                    while True:
                        try:
                            line = await asyncio.wait_for(
                                anext(line_iterator),
                                timeout=endpoint.upstream_timeout,
                            )
                        except StopAsyncIteration:
                            break
                        await session.queue.put(self._redactor.redact_known_secrets(line))
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, OSError, RuntimeError, UnicodeError, ValueError) as exc:
                logger.error(
                    "Error reading response from streamable-http backend: %s",
                    self._redactor.redact(str(exc)),
                )
            finally:
                try:
                    await stream_context.__aexit__(None, None, None)
                finally:
                    if on_complete is not None:
                        await on_complete()

        response_task = asyncio.create_task(
            process_response(),
            name=f"mcp-mux:{path_prefix}:legacy-response",
        )
        runtime.track_legacy_task(response_task)
        return Response("Accepted", status_code=202)

    async def _proxy_request(
        self,
        *,
        request: Request,
        endpoint: Endpoint,
        path_prefix: str,
        target_url: str,
        forward_headers: dict[str, str],
        request_body: bytes,
        policy: CapabilityPolicy,
        principal: str,
    ) -> Response:
        try:
            client = await self._get_http_client()
            stream_context = client.stream(
                method=request.method,
                url=target_url,
                headers=forward_headers,
                params=dict(request.query_params),
                content=request_body,
                timeout=_upstream_stream_timeout(endpoint.upstream_timeout),
            )
            response = await asyncio.wait_for(
                stream_context.__aenter__(),
                timeout=endpoint.upstream_timeout,
            )
        except (ClientDisconnect, httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            logger.error(
                "Proxy error for %s: %s",
                path_prefix,
                self._redactor.redact(str(exc)),
            )
            return JSONResponse({"error": "Upstream proxy request failed"}, status_code=502)

        content_type = response.headers.get("content-type", "")
        if "application/json" in content_type.casefold():
            try:
                body_bytes = await asyncio.wait_for(
                    response.aread(),
                    timeout=endpoint.upstream_timeout,
                )
            except TimeoutError:
                return JSONResponse({"error": "Upstream response timed out"}, status_code=504)
            finally:
                await stream_context.__aexit__(None, None, None)
            decoded_body = body_bytes.decode("utf-8", errors="replace")
            redacted_body = self._redactor.redact_known_secrets(decoded_body)
            projected_body, policy_changed = policy.project_json_text(
                redacted_body,
                principal=principal,
                endpoint=path_prefix,
            )
            body_transformed = policy_changed or redacted_body != decoded_body
            response_headers = self._safe_response_headers(
                response.headers,
                body_was_decoded=True,
                body_was_transformed=body_transformed,
            )
            if body_transformed:
                response_headers["Cache-Control"] = "private, no-store"
            return Response(
                content=projected_body.encode("utf-8"),
                status_code=response.status_code,
                headers=response_headers,
                media_type=content_type,
            )

        if "event-stream" in content_type.casefold():
            response_headers = self._safe_response_headers(
                response.headers,
                body_was_transformed=True,
            )
            response_headers.update(
                {
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                }
            )

            async def sse_content_generator() -> AsyncIterator[bytes]:
                try:
                    async for event in iter_sse_events(response.aiter_lines()):
                        def transform(data: str) -> str:
                            redacted = self._redactor.redact_known_secrets(data)
                            current_endpoint = self._configs.get(path_prefix, endpoint)
                            current_policy = CapabilityPolicy.from_endpoint(current_endpoint)
                            projected, _ = current_policy.project_json_text(
                                redacted,
                                principal=principal,
                                endpoint=path_prefix,
                            )
                            return projected

                        yield render_sse_event(transform_sse_event(event, transform))
                finally:
                    await stream_context.__aexit__(None, None, None)

            return StreamingResponse(
                sse_content_generator(),
                status_code=response.status_code,
                headers=response_headers,
                media_type=content_type,
            )

        textual_response = content_type.casefold().startswith("text/")
        response_headers = self._safe_response_headers(
            response.headers,
            body_was_transformed=textual_response and self._redactor.active,
        )
        if textual_response and self._redactor.active:
            response_headers["Cache-Control"] = "private, no-store"

        async def content_generator() -> AsyncIterator[bytes]:
            try:
                if textual_response and self._redactor.active:
                    iterator = response.aiter_text().__aiter__()
                    stream_redactor = self._redactor.stream()
                    while True:
                        try:
                            text = await asyncio.wait_for(
                                anext(iterator),
                                timeout=endpoint.upstream_timeout,
                            )
                        except StopAsyncIteration:
                            break
                        redacted = stream_redactor.feed(text)
                        if redacted:
                            yield redacted.encode("utf-8")
                    tail = stream_redactor.finish()
                    if tail:
                        yield tail.encode("utf-8")
                else:
                    iterator_bytes = response.aiter_bytes().__aiter__()
                    while True:
                        try:
                            chunk = await asyncio.wait_for(
                                anext(iterator_bytes),
                                timeout=endpoint.upstream_timeout,
                            )
                        except StopAsyncIteration:
                            break
                        yield chunk
            finally:
                await stream_context.__aexit__(None, None, None)

        return StreamingResponse(
            content_generator(),
            status_code=response.status_code,
            headers=response_headers,
            media_type=content_type,
        )

    async def catch_all_proxy(self, request: Request) -> Response:
        path_prefix = request.path_params.get("path_prefix")
        if not path_prefix or path_prefix not in self._runtimes:
            return JSONResponse(
                {"error": f"Endpoint '{path_prefix}' not configured"},
                status_code=404,
            )

        runtime = self._runtimes[path_prefix]
        endpoint = runtime.config
        subpath = request.path_params.get("subpath")
        if subpath is not None and endpoint.transport != "sse":
            return _transport_error_response(
                404,
                f"Streamable HTTP endpoint '/{path_prefix}' does not expose subpaths",
            )

        request_body = b""
        parsed_request: ParsedJSONRPCRequest | None = None
        request_era = ProtocolEra.LEGACY
        if request.method == "POST":
            body_result = await self._read_post_body(request)
            if isinstance(body_result, JSONResponse):
                return body_result
            request_body, parsed_request, request_era = body_result
            if request_era is ProtocolEra.MODERN and endpoint.transport != "streamable-http":
                return _transport_error_response(
                    400,
                    "MCP 2026-07-28 requests require the canonical Streamable HTTP endpoint",
                    request_id=parsed_request.request_id,
                )
            if request_era is ProtocolEra.MODERN and request.query_params.get("session_id"):
                return _transport_error_response(
                    400,
                    "MCP 2026-07-28 requests are stateless and cannot use a local bridge session",
                    request_id=parsed_request.request_id,
                )
        elif (
            endpoint.transport == "streamable-http"
            and request.headers.get("mcp-protocol-version") == MODERN_PROTOCOL_VERSION
        ):
            return _transport_error_response(
                405,
                "MCP 2026-07-28 Streamable HTTP uses POST only",
            )

        principal = self._principal(request)
        policy = CapabilityPolicy.from_endpoint(endpoint)
        request_name: str | None = None
        if parsed_request is not None:
            try:
                policy_names = extract_policy_request_names(parsed_request)
            except ProtocolRequestError as exc:
                return _jsonrpc_error_response(exc)
            request_name = policy_names[0] if len(policy_names) == 1 else None
            for policy_name in policy_names or (None,):
                decision = policy.authorize(
                    principal=principal,
                    endpoint=path_prefix,
                    method=parsed_request.method,
                    name=policy_name,
                )
                if not decision.allowed:
                    if parsed_request.request_id is None:
                        return Response(status_code=403)
                    return JSONResponse(
                        build_jsonrpc_error(
                            CAPABILITY_DENIED,
                            "Capability denied by endpoint policy",
                            request_id=parsed_request.request_id,
                        ),
                        status_code=403,
                    )

        tool_limit_name = (
            request_name
            if parsed_request is not None and parsed_request.method == "tools/call"
            else None
        )
        limit_lease, limit_rejection = await self._limiter.acquire(
            endpoint,
            capability_name=tool_limit_name,
        )
        if limit_rejection is not None:
            if parsed_request is None or parsed_request.request_id is None:
                return Response(status_code=429)
            return JSONResponse(
                build_jsonrpc_error(
                    REQUEST_LIMITED,
                    "Endpoint request limit exceeded",
                    request_id=parsed_request.request_id,
                    data={"scope": limit_rejection.scope},
                ),
                status_code=429,
            )

        request_path = request.url.path
        target_url = get_target_url(endpoint.url, request_path, path_prefix)
        if endpoint.transport == "sse" and request_path.startswith(f"/{path_prefix}/"):
            parsed_config = urlparse(endpoint.url)
            upstream_path = request_path[len(f"/{path_prefix}") :]
            target_url = urlunparse(
                (
                    parsed_config.scheme,
                    parsed_config.netloc,
                    upstream_path,
                    "",
                    "",
                    "",
                )
            )

        forward_headers = self._forward_headers(request, endpoint)
        is_get = request.method == "GET"
        is_sse_init = is_get and "text/event-stream" in request.headers.get("accept", "").casefold()

        if is_get and endpoint.transport == "streamable-http":
            if not is_sse_init:
                return await self._finish_leased_response(
                    _transport_error_response(
                        405,
                        "Use POST for Streamable HTTP JSON-RPC, or a supported legacy SSE GET",
                    ),
                    limit_lease,
                )
            if endpoint.legacy_sse_bridge:
                # Local compatibility SSE does not acquire an upstream lease, so
                # retirement admission must be checked explicitly. There is no
                # await between this state check and session publication.
                if runtime.state is RuntimeState.DRAINING:
                    return await self._finish_leased_response(
                        JSONResponse({"error": "Endpoint runtime is draining"}, status_code=503),
                        limit_lease,
                    )
                session_id = uuid4().hex
                queue: asyncio.Queue[str] = asyncio.Queue()
                self.active_sessions[session_id] = BridgeSession(
                    path_prefix=path_prefix,
                    queue=queue,
                )
                runtime.legacy_session_ids.add(session_id)
                return await self._finish_leased_response(
                    StreamingResponse(
                        self.local_sse_generator(session_id, queue, path_prefix),
                        media_type="text/event-stream",
                        headers={
                            "Cache-Control": "no-cache",
                            "Connection": "keep-alive",
                            "X-Accel-Buffering": "no",
                        },
                    ),
                    limit_lease,
                )

        session_id = request.query_params.get("session_id")
        if (
            request.method == "POST"
            and endpoint.transport == "streamable-http"
            and session_id
            and request_era is ProtocolEra.LEGACY
            and not endpoint.legacy_sse_bridge
        ):
            stale_session = self.active_sessions.get(session_id)
            if stale_session is not None and stale_session.path_prefix == path_prefix:
                self.active_sessions.pop(session_id, None)
                runtime.legacy_session_ids.discard(session_id)
            return await self._finish_leased_response(
                _transport_error_response(
                    400,
                    "Legacy bridge sessions are disabled for this endpoint",
                    request_id=None if parsed_request is None else parsed_request.request_id,
                ),
                limit_lease,
            )

        try:
            runtime_lease, activation_error = await self._acquire_upstream_lease(runtime)
        except BaseException:
            # Until runtime acquisition returns, only the request-limit lease is
            # owned here. Cancellation during managed demand-start must not retain it.
            await self._release_leases(limit_lease)
            raise
        if activation_error is not None:
            await self._release_leases(limit_lease)
            return activation_error

        if is_get and endpoint.transport == "sse" and is_sse_init:
            return await self._finish_leased_response(
                StreamingResponse(
                    self.sse_proxy_generator(
                        target_url,
                        forward_headers,
                        dict(request.query_params),
                        path_prefix,
                        endpoint.upstream_timeout,
                        principal,
                    ),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                ),
                limit_lease,
                runtime_lease,
            )

        if (
            request.method == "POST"
            and endpoint.transport == "streamable-http"
            and endpoint.legacy_sse_bridge
            and session_id
            and request_era is ProtocolEra.LEGACY
        ):
            # The request task owns both leases until _bridge_post() returns.
            # A successful return means the bridge either released them for an
            # early response or transferred ownership to its tracked response task.
            bridge_setup_owns_leases = True
            try:
                response = await self._bridge_post(
                    request=request,
                    endpoint=endpoint,
                    runtime=runtime,
                    path_prefix=path_prefix,
                    target_url=target_url,
                    forward_headers=forward_headers,
                    request_body=request_body,
                    session_id=session_id,
                    policy=policy,
                    principal=principal,
                    on_complete=lambda: self._release_leases(limit_lease, runtime_lease),
                )
                bridge_setup_owns_leases = False
                return response
            finally:
                if bridge_setup_owns_leases:
                    await self._release_leases(limit_lease, runtime_lease)

        try:
            response = await self._proxy_request(
                request=request,
                endpoint=endpoint,
                path_prefix=path_prefix,
                target_url=target_url,
                forward_headers=forward_headers,
                request_body=request_body,
                policy=policy,
                principal=principal,
            )
            return await self._finish_leased_response(response, limit_lease, runtime_lease)
        except BaseException:
            await self._release_leases(limit_lease, runtime_lease)
            raise


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")


@asynccontextmanager
async def lifespan(app: Starlette):
    del app
    logger.info("Initializing MCP Router Lifespan...")
    await router.open_http_client()
    router._accepting_work = True
    watcher = ConfigWatcher(CONFIG_PATH, router.apply_configuration)
    try:
        await watcher.start()
        router._running = True
        router._checker_task = asyncio.create_task(
            router.idle_timeout_checker(),
            name="mcp-mux:idle-timeout-checker",
        )
        yield
    finally:
        logger.info("Shutting down MCP Router Lifespan...")
        router._accepting_work = False
        if router._checker_task:
            router._checker_task.cancel()
            try:
                await router._checker_task
            except asyncio.CancelledError:
                pass
            router._checker_task = None
        await watcher.stop()
        await router.process_manager.cleanup(list(router._runtimes.values()))
        await router.close_http_client()
        router._running = False


app = Starlette(lifespan=lifespan)
router = MCPRouter(app, CONFIG_PATH)
