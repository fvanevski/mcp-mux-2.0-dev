from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.requests import Request

from mcp_router.core.config_loader import EndpointConfig
from mcp_router.server import router


@pytest.fixture(autouse=True)
def _reset_router():
    router._configs = {}
    router._legacy_bridge = None
    yield
    router._configs = {}
    router._legacy_bridge = None


@pytest.mark.asyncio
async def test_streamable_http_plain_get_returns_json_not_upstream_html():
    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http",
        )
    }
    mock_req = MagicMock(spec=Request)
    mock_req.method = "GET"
    mock_req.path_params = {"path_prefix": "huggingface"}
    mock_req.url.path = "/huggingface"
    mock_req.headers = {"accept": "*/*"}
    mock_req.query_params = {}
    mock_req.scope = {}
    with patch("httpx.AsyncClient.stream") as mock_stream:
        response = await router.catch_all_proxy(mock_req)
    assert response.status_code == 405
    assert response.media_type == "application/json"
    assert b"Streamable HTTP" in response.body
    assert router._legacy_bridge is None
    mock_stream.assert_not_called()


@pytest.mark.asyncio
async def test_streamable_http_sse_get_preserves_upstream_streamable_http():
    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http",
        )
    }
    mock_req = MagicMock(spec=Request)
    mock_req.method = "GET"
    mock_req.path_params = {"path_prefix": "huggingface"}
    mock_req.url.path = "/huggingface"
    mock_req.headers = {"accept": "text/event-stream"}
    mock_req.query_params = {}
    mock_req.scope = {}
    mock_response = MagicMock()
    mock_response.status_code = 405
    mock_response.headers = httpx.Headers({"content-type": "application/json"})
    mock_response.aread = AsyncMock(return_value=b'{"error":"method not allowed"}')
    mock_stream = MagicMock()
    mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream.__aexit__ = AsyncMock(return_value=None)
    with patch("httpx.AsyncClient.stream", return_value=mock_stream) as mock_stream_call:
        response = await router.catch_all_proxy(mock_req)
    assert response.status_code == 405
    assert response.media_type == "application/json"
    assert router._legacy_bridge is None
    assert mock_stream_call.call_args.kwargs["method"] == "GET"
    assert mock_stream_call.call_args.kwargs["url"] == "https://huggingface.co/mcp"


@pytest.mark.asyncio
async def test_streamable_http_sse_get_opens_local_sse_bridge_when_enabled():
    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http",
            legacy_sse_bridge={},
        )
    }
    mock_req = MagicMock(spec=Request)
    mock_req.method = "GET"
    mock_req.path_params = {"path_prefix": "huggingface"}
    mock_req.url.path = "/huggingface"
    mock_req.headers = {"accept": "text/event-stream"}
    mock_req.query_params = {}
    mock_req.scope = {}
    with patch("httpx.AsyncClient.stream") as mock_stream:
        response = await router.catch_all_proxy(mock_req)
    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert router._legacy_bridge is not None
    assert len(router._legacy_bridge.sessions) == 1
    session = next(iter(router._legacy_bridge.sessions.values()))
    assert session.path_prefix == "huggingface"
    mock_stream.assert_not_called()
    await router._legacy_bridge.close_all()


@pytest.mark.asyncio
async def test_streamable_http_direct_response_json_bridge():
    endpoint = EndpointConfig(
        path="huggingface",
        mode="remote",
        url="https://huggingface.co/mcp",
        summary="HuggingFace remote server",
        transport="streamable-http",
        legacy_sse_bridge={},
    )
    router._configs = {"huggingface": endpoint}
    bridge = router._get_legacy_bridge()
    bridge.open_session(endpoint=endpoint, runtime=router._runtimes["huggingface"])
    session = next(iter(bridge.sessions.values()))
    local_session_id = session.session_id
    queue = session.queue

    mock_init_resp = MagicMock()
    mock_init_resp.status_code = 200
    mock_init_resp.headers = httpx.Headers(
        {"content-type": "application/json", "mcp-session-id": "remote-session-456"}
    )
    mock_init_resp.aread = AsyncMock(
        return_value=b'{"jsonrpc":"2.0","result":{"protocolVersion":"2024-11-05"},"id":1}'
    )
    mock_init_stream = MagicMock()
    mock_init_stream.__aenter__ = AsyncMock(return_value=mock_init_resp)
    mock_init_stream.__aexit__ = AsyncMock(return_value=None)

    mock_req_init = MagicMock(spec=Request)
    mock_req_init.method = "POST"
    mock_req_init.path_params = {"path_prefix": "huggingface"}
    mock_req_init.url.path = "/huggingface"
    mock_req_init.headers = {"accept": "application/json", "content-type": "application/json"}
    mock_req_init.query_params = {"session_id": local_session_id}
    mock_req_init.body = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":1,"method":"initialize"}')
    mock_req_init.scope = {}

    with patch("httpx.AsyncClient.stream", return_value=mock_init_stream) as mock_stream_call:
        response = await router.catch_all_proxy(mock_req_init)
    assert response.status_code == 202
    assert mock_stream_call.call_args.kwargs["headers"].get("Mcp-Session-Id") is None
    assert bridge.sessions[local_session_id].remote_session_id == "remote-session-456"
    assert await queue.get() == b'event: message\ndata: {"jsonrpc":"2.0","result":{"protocolVersion":"2024-11-05"},"id":1}\n\n'
    await bridge.close_all()


@pytest.mark.asyncio
async def test_streamable_http_rejects_session_for_different_endpoint():
    weather = EndpointConfig(
        path="weather",
        mode="remote",
        url="http://api.weather.com/mcp",
        summary="Weather summary",
        transport="streamable-http",
        legacy_sse_bridge={},
    )
    huggingface = EndpointConfig(
        path="huggingface",
        mode="remote",
        url="https://huggingface.co/mcp",
        summary="HuggingFace remote server",
        transport="streamable-http",
        legacy_sse_bridge={},
    )
    router._configs = {"weather": weather, "huggingface": huggingface}
    bridge = router._get_legacy_bridge()
    bridge.open_session(endpoint=weather, runtime=router._runtimes["weather"])
    session = next(iter(bridge.sessions.values()))
    session.remote_session_id = "remote-weather-session"
    mock_req = MagicMock(spec=Request)
    mock_req.method = "POST"
    mock_req.path_params = {"path_prefix": "huggingface"}
    mock_req.url.path = "/huggingface"
    mock_req.headers = {"accept": "application/json", "content-type": "application/json"}
    mock_req.query_params = {"session_id": session.session_id}
    mock_req.body = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')
    mock_req.scope = {}
    with patch("httpx.AsyncClient.stream") as mock_stream:
        response = await router.catch_all_proxy(mock_req)
    assert response.status_code == 409
    assert b"different endpoint" in response.body
    mock_stream.assert_not_called()
    await bridge.close_all()
