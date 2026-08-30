from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncGenerator, AsyncIterator, Awaitable, Callable, Mapping
from contextlib import asynccontextmanager
from dataclasses import dataclass
from urllib.parse import urlparse, urlunparse
from uuid import uuid4

import httpx
import yaml
from starlette.applications import Starlette
from starlette.requests import ClientDisconnect, Request
from starlette.responses import JSONResponse, Response, StreamingResponse

from mcp_router.core.config_loader import (
    ConfigWatcher,
    Endpoint,
    ManagedEndpointConfig,
    RouterConfig,
    SecurityConfig,
    load_router_config,
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
    extract_request_name,
    parse_jsonrpc_request,
    validate_protocol_request,
)
from mcp_router.core.redaction import SecretRedactor
from mcp_router.core.security import (
    GatewaySecurityMiddleware,
    build_upstream_headers,
    sanitize_response_headers,
)
from mcp_router.core.sse import iter_sse_events, render_sse_event, transform_sse_event

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


@dataclass
class BridgeSession:
    path_prefix: str
    queue: asyncio.Queue[str]
    remote_session_id: str | None = None


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


class MCPRouter:
    def __init__(self, app: Starlette, config_path: str):
        self.app = app
        self.config_path = config_path
        self.process_manager = ProcessManager()
        self._configs: dict[str, Endpoint] = {}
        self.max_request_body_bytes = _DEFAULT_MAX_REQUEST_BODY_BYTES
        self.last_activity: dict[str, float] = {}
        self.active_connections: dict[str, int] = {}
        self.locks: dict[str, asyncio.Lock] = {}
        self.active_sessions: dict[str, BridgeSession] = {}
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

    def apply_configuration(self, config: RouterConfig) -> None:
        new_endpoints = {endpoint.path: endpoint for endpoint in config.endpoints}
        self.max_request_body_bytes = config.max_request_body_bytes
        self.security_config = config.security
        self._redactor = SecretRedactor.from_router_config(config)
        self.process_manager.set_redactor(self._redactor.redact)

        to_stop: list[str] = []
        for path in list(self._configs):
            if path not in new_endpoints:
                to_stop.append(path)
            elif _endpoint_requires_runtime_reset(self._configs[path], new_endpoints[path]):
                logger.info("Runtime-affecting config for %s changed, stopping it.", path)
                to_stop.append(path)

        for path in to_stop:
            asyncio.create_task(self.process_manager.stop_managed_server(path))
            self._configs.pop(path, None)
            self.last_activity.pop(path, None)
            self.locks.pop(path, None)
            self._drop_sessions_for_path(path)

        for path, endpoint in new_endpoints.items():
            self._configs[path] = endpoint
            if path not in self.locks:
                self.locks[path] = asyncio.Lock()

        logger.info("Applied config. Active paths: %s", list(self._configs))

    def _drop_sessions_for_path(self, path_prefix: str) -> None:
        session_ids = [
            session_id
            for session_id, session in self.active_sessions.items()
            if session.path_prefix == path_prefix
        ]
        for session_id in session_ids:
            self.active_sessions.pop(session_id, None)

    async def idle_timeout_checker(self) -> None:
        while self._running:
            try:
                await asyncio.sleep(10)
                current_time = time.time()
                for path, endpoint in list(self._configs.items()):
                    if endpoint.mode == "managed_cli" and self.process_manager.is_running(path):
                        active_conns = self.active_connections.get(path, 0)
                        if active_conns > 0:
                            self.last_activity[path] = current_time

                        last_activity = self.last_activity.get(path, 0)
                        if current_time - last_activity > endpoint.timeout:
                            logger.info(
                                "Inactivity timeout (%ss) exceeded for %s. Stopping process.",
                                endpoint.timeout,
                                path,
                            )
                            await self.process_manager.stop_managed_server(path)
            except asyncio.CancelledError:
                break
            except (OSError, RuntimeError, ValueError) as exc:
                logger.error("Error in idle timeout checker: %s", exc)

    async def get_summary(self, request: Request) -> JSONResponse:
        del request
        summary_list = [
            {
                "path": config.path,
                "mode": config.mode,
                "summary": config.summary,
            }
            for config in self._configs.values()
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

    async def _ensure_managed_server(self, path_prefix: str, endpoint: Endpoint) -> JSONResponse | None:
        if endpoint.mode != "managed_cli":
            return None
        async with self.locks[path_prefix]:
            if self.process_manager.is_running(path_prefix):
                return None
            logger.info("On-demand activation triggered for: %s", path_prefix)
            try:
                await self.process_manager.start_managed_server(endpoint)
            except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
                logger.error("Failed to start managed server %s: %s", path_prefix, exc)
                return JSONResponse(
                    {"error": f"Failed to start managed server: {exc}"},
                    status_code=500,
                )
        return None

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

    @staticmethod
    def _principal(request: Request) -> str:
        scope = getattr(request, "scope", {})
        if isinstance(scope, dict):
            principal = scope.get("mcp.principal")
            if isinstance(principal, str) and principal:
                return principal
        return "local"

    async def _finish_limited_response(
        self,
        response: Response,
        lease: LimitLease | None,
    ) -> Response:
        if lease is None:
            return response
        if not isinstance(response, StreamingResponse):
            await lease.release()
            return response

        original_iterator = response.body_iterator

        async def limited_iterator() -> AsyncIterator[bytes | str]:
            try:
                async for chunk in original_iterator:
                    yield chunk
            finally:
                await lease.release()

        response.body_iterator = limited_iterator()
        return response

    async def sse_proxy_generator(
        self,
        target_url: str,
        headers: Mapping[str, str],
        params: dict[str, str],
        path_prefix: str,
        timeout: float,
    ) -> AsyncIterator[bytes]:
        self.active_connections[path_prefix] = self.active_connections.get(path_prefix, 0) + 1
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
                self.last_activity[path_prefix] = time.time()

                def transform(data: str) -> str:
                    rewritten = _rewrite_legacy_endpoint_data(data, path_prefix)
                    endpoint = self._configs.get(path_prefix)
                    if endpoint is None:
                        return rewritten
                    return filter_tools_response(
                        rewritten,
                        endpoint.allowed_tools,
                        endpoint.denied_tools,
                    )

                yield render_sse_event(transform_sse_event(event, transform))
        finally:
            if response is not None:
                await stream_context.__aexit__(None, None, None)
            self.active_connections[path_prefix] = max(
                0,
                self.active_connections.get(path_prefix, 0) - 1,
            )
            self.last_activity[path_prefix] = time.time()

    async def local_sse_generator(
        self,
        session_id: str,
        queue: asyncio.Queue[str],
        path_prefix: str,
    ) -> AsyncGenerator[bytes]:
        self.active_connections[path_prefix] = self.active_connections.get(path_prefix, 0) + 1
        client_post_uri = f"/{path_prefix}?session_id={session_id}"
        yield f"event: endpoint\ndata: {client_post_uri}\n\n".encode()
        try:
            while True:
                line = await queue.get()
                yield (line + "\n").encode("utf-8")
        finally:
            self.active_sessions.pop(session_id, None)
            self.active_connections[path_prefix] = max(
                0,
                self.active_connections.get(path_prefix, 0) - 1,
            )
            self.last_activity[path_prefix] = time.time()

    async def _bridge_post(
        self,
        *,
        request: Request,
        endpoint: Endpoint,
        path_prefix: str,
        target_url: str,
        forward_headers: dict[str, str],
        request_body: bytes,
        session_id: str,
    ) -> Response:
        session = self.active_sessions.get(session_id)
        if session is None:
            return JSONResponse({"error": "Session not found"}, status_code=404)
        if session.path_prefix != path_prefix:
            return JSONResponse({"error": "Session belongs to a different endpoint"}, status_code=409)

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
            logger.error("Failed to proxy bridge POST to streamable-http backend: %s", exc)
            return JSONResponse({"error": f"Failed to proxy request: {exc}"}, status_code=502)

        remote_session_id = response.headers.get("mcp-session-id")
        if remote_session_id:
            session.remote_session_id = remote_session_id

        async def process_response() -> None:
            try:
                content_type = response.headers.get("content-type", "").casefold()
                if "application/json" in content_type:
                    body_bytes = await asyncio.wait_for(
                        response.aread(),
                        timeout=endpoint.upstream_timeout,
                    )
                    body_str = body_bytes.decode("utf-8", errors="replace")
                    filtered_body = filter_tools_response(
                        body_str,
                        endpoint.allowed_tools,
                        endpoint.denied_tools,
                    )
                    await session.queue.put("event: message")
                    await session.queue.put(f"data: {filtered_body}")
                    await session.queue.put("")
                elif "text/event-stream" in content_type or "event-stream" in content_type:
                    async for event in iter_sse_events(response.aiter_lines()):
                        transformed = transform_sse_event(
                            event,
                            lambda data: filter_tools_response(
                                data,
                                endpoint.allowed_tools,
                                endpoint.denied_tools,
                            ),
                        )
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
                        await session.queue.put(line)
            except asyncio.CancelledError:
                raise
            except (httpx.HTTPError, OSError, RuntimeError, UnicodeError, ValueError) as exc:
                logger.error("Error reading response from streamable-http backend: %s", exc)
            finally:
                await stream_context.__aexit__(None, None, None)

        asyncio.create_task(process_response())
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
            logger.error("Proxy error for %s: %s", path_prefix, exc)
            return JSONResponse({"error": f"Proxy error: {exc}"}, status_code=502)

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
            body_str = body_bytes.decode("utf-8", errors="replace")
            filtered_body = filter_tools_response(
                body_str,
                endpoint.allowed_tools,
                endpoint.denied_tools,
            )
            response_headers = build_response_headers(
                response.headers,
                _HOP_BY_HOP_REQUEST_HEADERS,
                body_was_decoded=True,
            )
            return Response(
                content=filtered_body.encode("utf-8"),
                status_code=response.status_code,
                headers=response_headers,
                media_type=content_type,
            )

        if "event-stream" in content_type.casefold():
            response_headers = build_response_headers(
                response.headers,
                _HOP_BY_HOP_REQUEST_HEADERS,
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
                        transformed = transform_sse_event(
                            event,
                            lambda data: filter_tools_response(
                                data,
                                endpoint.allowed_tools,
                                endpoint.denied_tools,
                            ),
                        )
                        yield render_sse_event(transformed)
                finally:
                    await stream_context.__aexit__(None, None, None)

            return StreamingResponse(
                sse_content_generator(),
                status_code=response.status_code,
                headers=response_headers,
                media_type=content_type,
            )

        response_headers = build_response_headers(
            response.headers,
            _HOP_BY_HOP_REQUEST_HEADERS,
        )

        async def content_generator() -> AsyncIterator[bytes]:
            iterator = response.aiter_bytes().__aiter__()
            try:
                while True:
                    try:
                        chunk = await asyncio.wait_for(
                            anext(iterator),
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
        if not path_prefix or path_prefix not in self._configs:
            return JSONResponse(
                {"error": f"Endpoint '{path_prefix}' not configured"},
                status_code=404,
            )

        endpoint = self._configs[path_prefix]
        subpath = request.path_params.get("subpath")
        if subpath is not None and endpoint.transport != "sse":
            return _transport_error_response(
                404,
                f"Streamable HTTP endpoint '/{path_prefix}' does not expose subpaths",
            )

        self.last_activity[path_prefix] = time.time()

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

        activation_error = await self._ensure_managed_server(path_prefix, endpoint)
        if activation_error is not None:
            return activation_error

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
                return _transport_error_response(
                    405,
                    "Use POST for Streamable HTTP JSON-RPC, or a supported legacy SSE GET",
                )
            if endpoint.legacy_sse_bridge:
                session_id = uuid4().hex
                queue: asyncio.Queue[str] = asyncio.Queue()
                self.active_sessions[session_id] = BridgeSession(
                    path_prefix=path_prefix,
                    queue=queue,
                )
                return StreamingResponse(
                    self.local_sse_generator(session_id, queue, path_prefix),
                    media_type="text/event-stream",
                    headers={
                        "Cache-Control": "no-cache",
                        "Connection": "keep-alive",
                        "X-Accel-Buffering": "no",
                    },
                )

        if is_get and endpoint.transport == "sse" and is_sse_init:
            return StreamingResponse(
                self.sse_proxy_generator(
                    target_url,
                    forward_headers,
                    dict(request.query_params),
                    path_prefix,
                    endpoint.upstream_timeout,
                ),
                media_type="text/event-stream",
                headers={
                    "Cache-Control": "no-cache",
                    "Connection": "keep-alive",
                    "X-Accel-Buffering": "no",
                },
            )

        session_id = request.query_params.get("session_id")
        if (
            request.method == "POST"
            and endpoint.transport == "streamable-http"
            and session_id
            and request_era is ProtocolEra.LEGACY
        ):
            return await self._bridge_post(
                request=request,
                endpoint=endpoint,
                path_prefix=path_prefix,
                target_url=target_url,
                forward_headers=forward_headers,
                request_body=request_body,
                session_id=session_id,
            )

        return await self._proxy_request(
            request=request,
            endpoint=endpoint,
            path_prefix=path_prefix,
            target_url=target_url,
            forward_headers=forward_headers,
            request_body=request_body,
        )


BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE_DIR, "config.yaml")


@asynccontextmanager
async def lifespan(app: Starlette):
    del app
    logger.info("Initializing MCP Router Lifespan...")
    await router.open_http_client()
    watcher = ConfigWatcher(CONFIG_PATH, router.apply_configuration)
    try:
        await watcher.start()

        if os.path.exists(CONFIG_PATH):
            try:
                initial_config = load_router_config(CONFIG_PATH)
                router.apply_configuration(initial_config)
            except (OSError, RuntimeError, ValueError, yaml.YAMLError) as exc:
                logger.error("Failed to apply initial configuration: %s", exc)

        router._running = True
        router._checker_task = asyncio.create_task(router.idle_timeout_checker())
        yield
    finally:
        logger.info("Shutting down MCP Router Lifespan...")
        router._running = False
        if router._checker_task:
            router._checker_task.cancel()
            try:
                await router._checker_task
            except asyncio.CancelledError:
                pass
            router._checker_task = None
        await watcher.stop()
        await router.process_manager.cleanup()
        await router.close_http_client()


app = Starlette(lifespan=lifespan)
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)
router = MCPRouter(app, CONFIG_PATH)
