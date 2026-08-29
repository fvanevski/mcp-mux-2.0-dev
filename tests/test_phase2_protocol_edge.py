from __future__ import annotations

import asyncio
import base64
import json
from collections.abc import AsyncGenerator, AsyncIterator
from typing import Any, cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse, StreamingResponse

from mcp_router.core.config_loader import EndpointConfig, RouterConfig
from mcp_router.core.protocol import (
    CLIENT_CAPABILITIES_META,
    HEADER_MISMATCH,
    INVALID_PARAMS,
    INVALID_REQUEST,
    MODERN_PROTOCOL_VERSION,
    PARSE_ERROR,
    PROTOCOL_VERSION_META,
    UNSUPPORTED_PROTOCOL_VERSION,
    ProtocolEra,
    ProtocolRequestError,
    parse_jsonrpc_request,
    validate_protocol_request,
)
from mcp_router.core.sse import iter_sse_events, render_sse_event, transform_sse_event
from mcp_router.server import MCPRouter


def modern_body(method: str = "tools/list", params: dict[str, object] | None = None) -> dict[str, Any]:
    actual_params = dict(params or {})
    actual_params["_meta"] = {
        PROTOCOL_VERSION_META: MODERN_PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META: {},
        "extension/meta": {"kept": True},
    }
    return {
        "jsonrpc": "2.0",
        "id": 1,
        "method": method,
        "params": actual_params,
        "extension": {"kept": True},
    }


def modern_headers(method: str = "tools/list", name: str | None = None) -> dict[str, str]:
    headers = {
        "content-type": "application/json; charset=utf-8",
        "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
        "Mcp-Method": method,
    }
    if name is not None:
        headers["Mcp-Name"] = name
    return headers


def test_parse_rejects_malformed_batch_and_missing_jsonrpc():
    with pytest.raises(ProtocolRequestError) as malformed:
        parse_jsonrpc_request(b"{")
    assert malformed.value.code == PARSE_ERROR

    with pytest.raises(ProtocolRequestError) as batch:
        parse_jsonrpc_request(b'[{"jsonrpc":"2.0","method":"tools/list"}]')
    assert batch.value.code == INVALID_REQUEST

    with pytest.raises(ProtocolRequestError) as repaired:
        parse_jsonrpc_request(b'{"id":1,"method":"tools/list","params":{}}')
    assert repaired.value.code == INVALID_REQUEST


def test_protocol_validation_preserves_legacy_and_enforces_modern_headers():
    legacy = parse_jsonrpc_request(
        b'{"jsonrpc":"2.0","id":1,"method":"initialize","params":{"protocolVersion":"2025-11-25"}}'
    )
    assert validate_protocol_request(
        legacy,
        {"MCP-Protocol-Version": "2025-11-25"},
    ) is ProtocolEra.LEGACY

    request = parse_jsonrpc_request(json.dumps(modern_body()).encode())
    with pytest.raises(ProtocolRequestError) as missing_method:
        validate_protocol_request(
            request,
            {"MCP-Protocol-Version": MODERN_PROTOCOL_VERSION},
        )
    assert missing_method.value.code == HEADER_MISMATCH


def test_modern_name_validation_supports_base64_sentinel():
    uri = "file:///資料/plan.txt"
    request = parse_jsonrpc_request(json.dumps(modern_body("resources/read", {"uri": uri})).encode())
    encoded = base64.b64encode(uri.encode()).decode()
    headers = modern_headers("resources/read", f"=?base64?{encoded}?=")
    assert validate_protocol_request(request, headers) is ProtocolEra.MODERN


def test_modern_missing_meta_and_unsupported_version_are_distinct():
    missing = parse_jsonrpc_request(b'{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}')
    with pytest.raises(ProtocolRequestError) as missing_error:
        validate_protocol_request(missing, modern_headers())
    assert missing_error.value.code == INVALID_PARAMS

    unsupported = modern_body()
    meta = unsupported["params"]["_meta"]
    meta[PROTOCOL_VERSION_META] = "2099-01-01"
    request = parse_jsonrpc_request(json.dumps(unsupported).encode())
    headers = modern_headers()
    headers["MCP-Protocol-Version"] = "2099-01-01"
    with pytest.raises(ProtocolRequestError) as unsupported_error:
        validate_protocol_request(request, headers)
    assert unsupported_error.value.code == UNSUPPORTED_PROTOCOL_VERSION
    assert unsupported_error.value.data == {"supportedVersions": [MODERN_PROTOCOL_VERSION]}


@pytest.mark.asyncio
async def test_sse_event_parser_preserves_comments_ids_retry_multiline_and_boundaries():
    async def lines():
        for line in [
            ": keepalive",
            "id: 17",
            "retry: 2500",
            "event: message",
            "data: first",
            "data: second",
            "",
            ": next",
            "data: unchanged",
            "",
        ]:
            yield line

    events = [event async for event in iter_sse_events(lines())]
    assert events == [
        [": keepalive", "id: 17", "retry: 2500", "event: message", "data: first", "data: second"],
        [": next", "data: unchanged"],
    ]
    transformed = transform_sse_event(events[0], lambda value: value.upper())
    assert transformed == [
        ": keepalive",
        "id: 17",
        "retry: 2500",
        "event: message",
        "data: FIRST",
        "data: SECOND",
    ]
    assert render_sse_event(transformed).endswith(b"\n\n")


@pytest.mark.asyncio
async def test_shared_http_client_lifecycle_constructs_once_and_closes_once():
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    fake = MagicMock()
    fake.is_closed = False
    fake.aclose = AsyncMock()
    with patch("mcp_router.server.httpx.AsyncClient", return_value=fake) as constructor:
        assert await isolated.open_http_client() is fake
        assert await isolated.open_http_client() is fake
        constructor.assert_called_once()
        await isolated.close_http_client()
    fake.aclose.assert_awaited_once()
    assert isolated._http_client is None


def test_transport_configuration_exposes_body_limit_and_endpoint_timeout():
    config = RouterConfig.model_validate(
        {
            "max_request_body_bytes": 2048,
            "endpoints": [
                {
                    "path": "example",
                    "mode": "remote",
                    "url": "https://example.test/mcp",
                    "summary": "Example",
                    "upstream_timeout": 12.5,
                }
            ],
        }
    )
    assert config.max_request_body_bytes == 2048
    assert config.endpoints[0].upstream_timeout == 12.5


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("body", "headers", "expected_status", "expected_code"),
    [
        (b"{", {"content-type": "application/json"}, 400, PARSE_ERROR),
        (
            b'[{"jsonrpc":"2.0","id":1,"method":"tools/list","params":{}}]',
            {"content-type": "application/json"},
            400,
            INVALID_REQUEST,
        ),
        (
            b'{"id":1,"method":"tools/list","params":{}}',
            {"content-type": "application/json"},
            400,
            INVALID_REQUEST,
        ),
        (
            json.dumps(modern_body()).encode(),
            {**modern_headers(), "Mcp-Method": "prompts/list"},
            400,
            HEADER_MISMATCH,
        ),
        (
            json.dumps(modern_body("tools/call", {"name": "actual", "arguments": {}})).encode(),
            modern_headers("tools/call", "different"),
            400,
            HEADER_MISMATCH,
        ),
    ],
)
async def test_invalid_streamable_requests_are_rejected_before_upstream(
    body: bytes,
    headers: dict[str, str],
    expected_status: int,
    expected_code: int,
):
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
            transport="streamable-http",
        )
    }
    request = MagicMock(spec=Request)
    request.method = "POST"
    request.path_params = {"path_prefix": "example"}
    request.url.path = "/example"
    request.headers = headers
    request.query_params = {}
    request.body = AsyncMock(return_value=body)
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.stream = MagicMock()
    isolated._http_client = cast(httpx.AsyncClient, fake_client)

    response = await isolated.catch_all_proxy(request)
    assert isinstance(response, JSONResponse)
    assert response.status_code == expected_status
    assert json.loads(bytes(response.body))["error"]["code"] == expected_code
    fake_client.stream.assert_not_called()


@pytest.mark.asyncio
async def test_wrong_content_type_and_oversize_are_rejected_before_upstream():
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated.max_request_body_bytes = 64
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
            transport="streamable-http",
        )
    }
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.stream = MagicMock()
    isolated._http_client = cast(httpx.AsyncClient, fake_client)

    for headers, body, status in [
        ({"content-type": "text/plain"}, b"{}", 415),
        ({"content-type": "application/json", "content-length": "65"}, b"{}", 413),
    ]:
        request = MagicMock(spec=Request)
        request.method = "POST"
        request.path_params = {"path_prefix": "example"}
        request.url.path = "/example"
        request.headers = headers
        request.query_params = {}
        request.body = AsyncMock(return_value=body)
        response = await isolated.catch_all_proxy(request)
        assert response.status_code == status
    fake_client.stream.assert_not_called()


@pytest.mark.asyncio
async def test_chunked_request_body_is_bounded_while_reading():
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated.max_request_body_bytes = 64
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
            transport="streamable-http",
        )
    }
    messages = [
        {"type": "http.request", "body": b"a" * 40, "more_body": True},
        {"type": "http.request", "body": b"b" * 40, "more_body": False},
    ]

    async def receive() -> dict[str, Any]:
        return messages.pop(0)

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": "POST",
        "scheme": "http",
        "path": "/example",
        "raw_path": b"/example",
        "query_string": b"",
        "headers": [(b"content-type", b"application/json")],
        "client": ("127.0.0.1", 1),
        "server": ("test", 80),
        "path_params": {"path_prefix": "example"},
    }
    request = Request(scope, receive)
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.stream = MagicMock()
    isolated._http_client = cast(httpx.AsyncClient, fake_client)

    response = await isolated.catch_all_proxy(request)
    assert response.status_code == 413
    fake_client.stream.assert_not_called()


@pytest.mark.asyncio
async def test_valid_modern_request_forwards_original_bytes_unknown_fields_and_timeout():
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
            transport="streamable-http",
            upstream_timeout=12.5,
        )
    }
    payload = modern_body("server/discover")
    original = json.dumps(payload, separators=(", ", ": "), ensure_ascii=False).encode()
    request = MagicMock(spec=Request)
    request.method = "POST"
    request.path_params = {"path_prefix": "example"}
    request.url.path = "/example"
    request.headers = {**modern_headers("server/discover"), "Mcp-Session-Id": "legacy-header"}
    request.query_params = {}
    request.body = AsyncMock(return_value=original)

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.headers = httpx.Headers({"content-type": "application/json"})
    upstream_response.aread = AsyncMock(
        return_value=b'{"jsonrpc":"2.0","id":1,"result":{"resultType":"complete"}}'
    )
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=upstream_response)
    stream_context.__aexit__ = AsyncMock(return_value=None)
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.stream = MagicMock(return_value=stream_context)
    isolated._http_client = cast(httpx.AsyncClient, fake_client)

    response = await isolated.catch_all_proxy(request)
    assert response.status_code == 200
    kwargs = fake_client.stream.call_args.kwargs
    assert kwargs["content"] == original
    assert kwargs["url"] == "https://upstream.test/mcp"
    assert kwargs["timeout"] == 12.5
    assert kwargs["headers"]["MCP-Protocol-Version"] == MODERN_PROTOCOL_VERSION
    assert isolated.active_sessions == {}


@pytest.mark.asyncio
async def test_modern_subpath_is_rejected():
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated._configs = {
        "modern": EndpointConfig(
            path="modern",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Modern",
            transport="streamable-http",
        ),
        "legacy": EndpointConfig(
            path="legacy",
            mode="remote",
            url="https://upstream.test/mcp/sse",
            summary="Legacy",
            transport="sse",
        ),
    }
    request = MagicMock(spec=Request)
    request.method = "POST"
    request.path_params = {"path_prefix": "modern", "subpath": "tools/list"}
    request.url.path = "/modern/tools/list"
    request.headers = modern_headers()
    request.query_params = {}
    request.body = AsyncMock(return_value=json.dumps(modern_body()).encode())
    response = await isolated.catch_all_proxy(request)
    assert response.status_code == 404


class EventStreamResponse:
    status_code = 200
    headers = httpx.Headers({"content-type": "text/event-stream"})

    async def aiter_lines(self) -> AsyncIterator[str]:
        yield ": keepalive"
        yield "id: 1"
        yield "event: message"
        yield 'data: {"jsonrpc":"2.0","id":1,"result":{"resultType":"complete"}}'
        yield ""
        await asyncio.Event().wait()


@pytest.mark.asyncio
async def test_downstream_stream_close_closes_upstream_response_context():
    isolated = MCPRouter(Starlette(), "/tmp/not-used.yaml")
    isolated._configs = {
        "example": EndpointConfig(
            path="example",
            mode="remote",
            url="https://upstream.test/mcp",
            summary="Example",
            transport="streamable-http",
        )
    }
    request = MagicMock(spec=Request)
    request.method = "POST"
    request.path_params = {"path_prefix": "example"}
    request.url.path = "/example"
    request.headers = modern_headers()
    request.query_params = {}
    request.body = AsyncMock(return_value=json.dumps(modern_body()).encode())

    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=EventStreamResponse())
    stream_context.__aexit__ = AsyncMock(return_value=None)
    fake_client = MagicMock()
    fake_client.is_closed = False
    fake_client.stream = MagicMock(return_value=stream_context)
    isolated._http_client = cast(httpx.AsyncClient, fake_client)

    response = await isolated.catch_all_proxy(request)
    assert isinstance(response, StreamingResponse)
    body_iterator = cast(AsyncGenerator[bytes, None], response.body_iterator)
    first_event = await anext(body_iterator)
    assert b": keepalive" in first_event
    assert b"id: 1" in first_event
    await body_iterator.aclose()
    stream_context.__aexit__.assert_awaited_once()
