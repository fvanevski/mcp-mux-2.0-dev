from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from mcp_router.core.config_loader import EndpointConfig, RouterConfig, SecurityConfig
from mcp_router.core.limits import RequestLimiter
from mcp_router.core.process_manager import ProcessManager
from mcp_router.core.protocol import (
    CAPABILITY_DENIED,
    CLIENT_CAPABILITIES_META,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META,
    REQUEST_LIMITED,
)
from mcp_router.core.redaction import SecretRedactor
from mcp_router.core.security import validate_bind_security
from mcp_router.server import BridgeSession, MCPRouter


def modern_body(method: str, params: dict[str, object] | None = None) -> dict[str, Any]:
    actual_params = dict(params or {})
    actual_params["_meta"] = {
        PROTOCOL_VERSION_META: MODERN_PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META: {},
    }
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": actual_params,
    }


def modern_headers(method: str, name: str | None = None) -> dict[str, str]:
    headers = {
        "content-type": "application/json",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def direct_request(
    *,
    body: dict[str, Any],
    headers: dict[str, str],
    path: str = "example",
) -> MagicMock:
    request = MagicMock(spec=Request)
    request.method = "POST"
    request.path_params = {"path_prefix": path}
    request.url.path = f"/{path}"
    request.headers = headers
    request.query_params = {}
    request.body = AsyncMock(return_value=json.dumps(body).encode())
    request.scope = {"mcp.principal": "test-principal"}
    return request


def json_upstream_response(
    body: bytes,
    *,
    headers: dict[str, str] | None = None,
) -> tuple[MagicMock, MagicMock]:
    response = MagicMock()
    response.status_code = 200
    response.headers = httpx.Headers(
        {"content-type": "application/json", **(headers or {})}
    )
    response.aread = AsyncMock(return_value=body)
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=response)
    stream_context.__aexit__ = AsyncMock(return_value=None)
    return response, stream_context


def fake_client_for(stream_context: MagicMock) -> tuple[httpx.AsyncClient, MagicMock]:
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.stream = MagicMock(return_value=stream_context)
    return cast(httpx.AsyncClient, fake_client), fake_client


def test_non_loopback_bind_requires_authenticated_mode() -> None:
    with pytest.raises(ValueError, match="Non-loopback binding"):
        validate_bind_security("0.0.0.0", SecurityConfig())

    validate_bind_security(
        "0.0.0.0",
        SecurityConfig(mode="authenticated", api_key="gateway-secret"),
    )


@pytest.mark.asyncio
async def test_invalid_host_and_origin_are_rejected_before_router_or_upstream() -> None:
    app = Starlette()
    isolated = MCPRouter(app, "/tmp/not-used.yaml")
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
        )
    }
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.stream = MagicMock()
    isolated._http_client = cast(httpx.AsyncClient, fake_client)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://evil.test") as client:
        invalid_host = await client.post(
            "/example",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
    assert invalid_host.status_code == 403

    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        invalid_origin = await client.post(
            "/example",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Origin": "https://evil.example"},
        )
    assert invalid_origin.status_code == 403
    fake_client.stream.assert_not_called()


@pytest.mark.asyncio
async def test_authenticated_gateway_consumes_caller_key_and_injects_upstream_key() -> None:
    app = Starlette()
    isolated = MCPRouter(app, "/tmp/not-used.yaml")
    config = RouterConfig.model_validate(
        {
            "security": {
                "mode": "authenticated",
                "allowed_hosts": ["localhost"],
                "api_key": "gateway-secret",
            },
            "endpoints": [
                {
                    "path": "example",
                    "mode": "remote",
                    "url": "https://upstream.test/mcp",
                    "summary": "Example",
                    "headers": {
                        "Authorization": "Bearer upstream-secret",
                        "X-Custom-Auth": "custom-secret",
                    },
                }
            ],
        }
    )
    isolated.apply_configuration(config)

    _, stream_context = json_upstream_response(
        b'{"jsonrpc":"2.0","id":1,"result":{"echo":"Bearer upstream-secret custom-secret"}}',
        headers={
            "etag": '"stale"',
            "digest": "sha-256=stale",
            "content-length": "999",
            "set-cookie": "upstream_session=opaque-upstream-cookie; HttpOnly",
        },
    )
    isolated._http_client, fake_client = fake_client_for(stream_context)

    transport = httpx.ASGITransport(app=app)
    async with httpx.AsyncClient(transport=transport, base_url="http://localhost") as client:
        missing = await client.post(
            "/example",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        )
        assert missing.status_code == 401

        accepted = await client.post(
            "/example",
            json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
            headers={"Authorization": "Bearer gateway-secret"},
        )

    assert accepted.status_code == 200
    assert "upstream-secret" not in accepted.text
    assert "custom-secret" not in accepted.text
    assert "[REDACTED]" in accepted.text
    assert accepted.headers.get("etag") is None
    assert accepted.headers.get("digest") is None
    assert accepted.headers.get("set-cookie") is None
    assert accepted.headers["cache-control"] == "private, no-store"

    upstream_headers = fake_client.stream.call_args.kwargs["headers"]
    assert upstream_headers["Authorization"] == "Bearer upstream-secret"
    assert upstream_headers["X-Custom-Auth"] == "custom-secret"
    assert "gateway-secret" not in repr(upstream_headers)


def test_short_configured_secret_is_redacted() -> None:
    redactor = SecretRedactor(["xy"])
    assert redactor.active
    assert redactor.redact("credential=xy") == "credential=[REDACTED]"


class SplitTextStreamResponse:
    status_code = 200
    headers = httpx.Headers({"content-type": "text/plain; charset=utf-8"})

    async def aiter_text(self) -> AsyncIterator[str]:
        yield "prefix upstream-"
        yield "secret suffix"


@pytest.mark.asyncio
async def test_text_stream_redaction_handles_secret_split_across_chunks() -> None:
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated.apply_configuration(
        RouterConfig.model_validate(
            {
                "endpoints": [
                    {
                        "path": "example",
                        "mode": "remote",
                        "url": "https://upstream.test/mcp",
                        "summary": "Example",
                        "headers": {"Authorization": "Bearer upstream-secret"},
                    }
                ]
            }
        )
    )
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=SplitTextStreamResponse())
    stream_context.__aexit__ = AsyncMock(return_value=None)
    isolated._http_client, _ = fake_client_for(stream_context)

    request = direct_request(
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"content-type": "application/json"},
    )
    response = await isolated.catch_all_proxy(request)

    assert isinstance(response, StreamingResponse)
    chunks = [chunk async for chunk in cast(AsyncIterator[bytes], response.body_iterator)]
    body = b"".join(chunks).decode("utf-8")
    assert "upstream-secret" not in body
    assert "[REDACTED]" in body
    assert body == "prefix [REDACTED] suffix"


@pytest.mark.asyncio
async def test_trusted_proxy_identity_requires_trusted_immediate_peer() -> None:
    app = Starlette()
    isolated = MCPRouter(app, "/tmp/not-used.yaml")
    isolated.security_config = SecurityConfig(
        mode="authenticated",
        allowed_hosts=["localhost"],
        trusted_proxies=["127.0.0.1/32"],
        trusted_proxy_identity_header="X-Forwarded-User",
    )

    trusted_transport = httpx.ASGITransport(app=app, client=("127.0.0.1", 50000))
    async with httpx.AsyncClient(
        transport=trusted_transport,
        base_url="http://localhost",
    ) as client:
        trusted = await client.get(
            "/missing",
            headers={"X-Forwarded-User": "alice"},
        )
    assert trusted.status_code == 404

    untrusted_transport = httpx.ASGITransport(app=app, client=("203.0.113.9", 50000))
    async with httpx.AsyncClient(
        transport=untrusted_transport,
        base_url="http://localhost",
    ) as client:
        untrusted = await client.get(
            "/missing",
            headers={"X-Forwarded-User": "alice"},
        )
    assert untrusted.status_code == 401


@pytest.mark.asyncio
async def test_denied_modern_tool_call_never_reaches_upstream() -> None:
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
            allowed_tools=["safe_tool"],
        )
    }
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.stream = MagicMock()
    isolated._http_client = cast(httpx.AsyncClient, fake_client)

    request = direct_request(
        body=modern_body(
            "tools/call",
            {"name": "hidden_tool", "arguments": {}},
        ),
        headers=modern_headers("tools/call", "hidden_tool"),
    )
    response = await isolated.catch_all_proxy(request)

    assert isinstance(response, JSONResponse)
    assert response.status_code == 403
    assert json.loads(bytes(response.body))["error"]["code"] == CAPABILITY_DENIED
    fake_client.stream.assert_not_called()


@pytest.mark.asyncio
async def test_denied_legacy_tool_call_uses_body_policy_and_never_reaches_upstream() -> None:
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
            denied_tools=["hidden_tool"],
        )
    }
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.stream = MagicMock()
    isolated._http_client = cast(httpx.AsyncClient, fake_client)

    request = direct_request(
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "hidden_tool", "arguments": {}},
        },
        headers={"content-type": "application/json"},
    )
    response = await isolated.catch_all_proxy(request)

    assert response.status_code == 403
    assert json.loads(bytes(response.body))["error"]["code"] == CAPABILITY_DENIED
    fake_client.stream.assert_not_called()


@pytest.mark.asyncio
async def test_denied_managed_tool_does_not_start_process() -> None:
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="managed_cli",
            argv=["example-mcp"],
            url="http://127.0.0.1:8123/mcp",
            summary="Example",
            allowed_tools=["safe_tool"],
        )
    }
    isolated.locks["example"] = asyncio.Lock()
    request = direct_request(
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "hidden_tool", "arguments": {}},
        },
        headers={"content-type": "application/json"},
    )

    with patch.object(
        isolated.process_manager,
        "start_managed_server",
        new=AsyncMock(),
    ) as start_managed:
        response = await isolated.catch_all_proxy(request)

    assert response.status_code == 403
    start_managed.assert_not_awaited()


@pytest.mark.asyncio
async def test_tools_list_projection_and_direct_call_share_policy_source() -> None:
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    endpoint = EndpointConfig(
        path="example",
        mode="remote",
        url="https://upstream.test/mcp",
        summary="Example",
        allowed_tools=["safe_tool"],
    )
    isolated._configs = {"example": endpoint}

    _, stream_context = json_upstream_response(
        b'{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"safe_tool"},{"name":"hidden_tool"}]}}'
    )
    isolated._http_client, fake_client = fake_client_for(stream_context)

    list_request = direct_request(
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"content-type": "application/json"},
    )
    list_response = await isolated.catch_all_proxy(list_request)
    assert list_response.status_code == 200
    tools = json.loads(bytes(list_response.body))["result"]["tools"]
    assert tools == [{"name": "safe_tool"}]
    assert fake_client.stream.call_count == 1

    denied_request = direct_request(
        body={
            "jsonrpc": "2.0",
            "id": 2,
            "method": "tools/call",
            "params": {"name": "hidden_tool", "arguments": {}},
        },
        headers={"content-type": "application/json"},
    )
    denied_response = await isolated.catch_all_proxy(denied_request)
    assert denied_response.status_code == 403
    assert fake_client.stream.call_count == 1


@pytest.mark.asyncio
async def test_policy_only_reload_does_not_restart_managed_process_or_drop_session() -> None:
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    previous = EndpointConfig(
        path="example",
        mode="managed_cli",
        argv=["example-mcp"],
        url="http://127.0.0.1:8123/mcp",
        summary="Example",
        allowed_tools=["tool_a"],
        legacy_sse_bridge=True,
    )
    isolated._configs = {"example": previous}
    isolated.locks["example"] = asyncio.Lock()
    isolated.active_sessions["session"] = BridgeSession(
        path_prefix="example",
        queue=asyncio.Queue(),
    )

    updated = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "example",
                    "mode": "managed_cli",
                    "argv": ["example-mcp"],
                    "url": "http://127.0.0.1:8123/mcp",
                    "summary": "Example",
                    "allowed_tools": ["tool_b"],
                    "legacy_sse_bridge": True,
                }
            ]
        }
    )

    with patch.object(
        isolated.process_manager,
        "stop_managed_server",
        new=AsyncMock(),
    ) as stop_managed:
        isolated.apply_configuration(updated)
        await asyncio.sleep(0)

    stop_managed.assert_not_awaited()
    assert "session" in isolated.active_sessions
    assert isolated._configs["example"].allowed_tools == ["tool_b"]


@pytest.mark.asyncio
async def test_endpoint_rate_limit_rejects_second_request_before_upstream() -> None:
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
            limits={"requests_per_minute": 1},
        )
    }

    _, first_context = json_upstream_response(
        b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
    )
    isolated._http_client, fake_client = fake_client_for(first_context)
    request = direct_request(
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"content-type": "application/json"},
    )

    first = await isolated.catch_all_proxy(request)
    second = await isolated.catch_all_proxy(request)

    assert first.status_code == 200
    assert second.status_code == 429
    assert json.loads(bytes(second.body))["error"]["code"] == REQUEST_LIMITED
    assert fake_client.stream.call_count == 1


@pytest.mark.asyncio
async def test_tool_rate_limit_is_scoped_to_named_tool() -> None:
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
            tool_limits={"expensive": {"requests_per_minute": 1}},
        )
    }

    _, first_context = json_upstream_response(
        b'{"jsonrpc":"2.0","id":1,"result":{"content":[]}}'
    )
    isolated._http_client, fake_client = fake_client_for(first_context)
    request = direct_request(
        body={
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/call",
            "params": {"name": "expensive", "arguments": {}},
        },
        headers={"content-type": "application/json"},
    )

    first = await isolated.catch_all_proxy(request)
    second = await isolated.catch_all_proxy(request)

    assert first.status_code == 200
    assert second.status_code == 429
    assert fake_client.stream.call_count == 1


@pytest.mark.asyncio
async def test_endpoint_and_tool_concurrency_limits_release_cleanly() -> None:
    limiter = RequestLimiter()
    endpoint_limited = EndpointConfig(
        path="endpoint-limited",
        mode="remote",
        url="https://upstream.test/mcp",
        summary="Endpoint limited",
        limits={"max_concurrent": 1},
    )

    endpoint_lease, endpoint_rejection = await limiter.acquire(endpoint_limited)
    assert endpoint_lease is not None
    assert endpoint_rejection is None
    duplicate_lease, duplicate_rejection = await limiter.acquire(endpoint_limited)
    assert duplicate_lease is None
    assert duplicate_rejection is not None
    assert duplicate_rejection.scope == "endpoint:endpoint-limited"

    await endpoint_lease.release()
    reacquired_lease, reacquired_rejection = await limiter.acquire(endpoint_limited)
    assert reacquired_lease is not None
    assert reacquired_rejection is None
    await reacquired_lease.release()

    tool_limited = EndpointConfig(
        path="tool-limited",
        mode="remote",
        url="https://upstream.test/mcp",
        summary="Tool limited",
        tool_limits={"expensive": {"max_concurrent": 1}},
    )
    tool_lease, tool_rejection = await limiter.acquire(
        tool_limited,
        capability_name="expensive",
    )
    assert tool_lease is not None
    assert tool_rejection is None
    duplicate_tool_lease, duplicate_tool_rejection = await limiter.acquire(
        tool_limited,
        capability_name="expensive",
    )
    assert duplicate_tool_lease is None
    assert duplicate_tool_rejection is not None
    assert duplicate_tool_rejection.scope == "tool:tool-limited:expensive"

    await tool_lease.release()


@pytest.mark.asyncio
async def test_managed_process_output_is_redacted(caplog: pytest.LogCaptureFixture) -> None:
    manager = ProcessManager()
    manager.set_redactor(SecretRedactor(["Bearer process-secret"]).redact)
    reader = asyncio.StreamReader()
    reader.feed_data(b"Authorization: Bearer process-secret\n")
    reader.feed_eof()
    caplog.set_level(logging.INFO)

    try:
        await manager._stream_logs(reader, "phase3-test")
    finally:
        manager.set_redactor(lambda text: text)

    assert "process-secret" not in caplog.text
    assert "[REDACTED]" in caplog.text


class OneEventStreamResponse:
    status_code = 200
    headers = httpx.Headers(
        {
            "content-type": "text/event-stream",
            "etag": '"stale"',
            "digest": "sha-256=stale",
        }
    )

    async def aiter_lines(self) -> AsyncIterator[str]:
        yield "event: message"
        yield 'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"safe"},{"name":"hidden"}]}}'
        yield ""


@pytest.mark.asyncio
async def test_policy_transformed_sse_drops_stale_entity_metadata() -> None:
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
            allowed_tools=["safe"],
        )
    }
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=OneEventStreamResponse())
    stream_context.__aexit__ = AsyncMock(return_value=None)
    isolated._http_client, _ = fake_client_for(stream_context)

    request = direct_request(
        body={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
        headers={"content-type": "application/json"},
    )
    response = await isolated.catch_all_proxy(request)

    assert isinstance(response, StreamingResponse)
    assert response.headers.get("etag") is None
    assert response.headers.get("digest") is None
    body_iterator = cast(AsyncIterator[bytes], response.body_iterator)
    body = await anext(body_iterator)
    assert b'"safe"' in body
    assert b'"hidden"' not in body
