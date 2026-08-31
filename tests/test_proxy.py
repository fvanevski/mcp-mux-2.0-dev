from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from httpx import AsyncClient

from mcp_router.core.config_loader import EndpointConfig
from mcp_router.server import app, router


@pytest.fixture(autouse=True)
def _reset_router():
    router._configs = {}
    router._legacy_bridge = None
    yield
    router._configs = {}
    router._legacy_bridge = None


@pytest.mark.asyncio
async def test_summary_route():
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://weather/mcp",
            summary="Weather summary",
        )
    }
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/summary")
    assert response.status_code == 200
    data = response.json()
    assert data["endpoints"][0]["path"] == "weather"
    assert data["endpoints"][0]["mode"] == "remote"
    assert data["endpoints"][0]["summary"] == "Weather summary"


@pytest.mark.asyncio
async def test_operational_routes_remain_gateway_owned() -> None:
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://weather/mcp",
            summary="Weather summary",
        )
    }
    async with AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        with patch("httpx.AsyncClient.stream") as upstream:
            summary = await client.get("/summary")
            metrics = await client.get("/metrics")

    assert summary.status_code == 200
    assert metrics.status_code == 200
    assert summary.json()["endpoints"][0]["path"] == "weather"
    assert metrics.json()["endpoints"][0]["path"] == "weather"
    upstream.assert_not_called()


@pytest.mark.asyncio
async def test_not_found_route():
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        response = await client.get("/nonexistent")
    assert response.status_code == 404
    assert "configured" in response.json()["error"]


@pytest.mark.asyncio
async def test_proxy_headers_forwarding():
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://api.weather.com/mcp",
            summary="Weather summary",
            inbound_headers=["X-Client-Header", "X-Override"],
            headers={
                "Authorization": "Bearer upstream-secret",
                "X-Custom-Auth": "secret-token",
                "X-Override": "router-value",
            },
        )
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"content-type": "application/json"})
    mock_response.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","result":{"tools":[]}}')
    mock_stream = MagicMock()
    mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream.__aexit__ = AsyncMock(return_value=None)

    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        with patch("httpx.AsyncClient.stream", return_value=mock_stream) as mock_stream_call:
            response = await client.post(
                "/weather",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={
                    "Authorization": "Bearer caller-secret",
                    "Cookie": "caller_session=caller-secret",
                    "Proxy-Authorization": "Bearer proxy-caller-secret",
                    "X-Forwarded-User": "spoofed-caller",
                    "X-Override": "client-value",
                    "X-Client-Header": "only-client",
                    "X-Unlisted": "must-not-forward",
                },
            )

    assert response.status_code == 200
    called_headers = mock_stream_call.call_args.kwargs["headers"]
    assert called_headers.get("X-Custom-Auth", called_headers.get("x-custom-auth")) == "secret-token"
    assert called_headers.get("X-Override", called_headers.get("x-override")) == "router-value"
    assert called_headers.get("X-Client-Header", called_headers.get("x-client-header")) == "only-client"
    assert called_headers.get("Authorization", called_headers.get("authorization")) == "Bearer upstream-secret"
    for forbidden in ("cookie", "proxy-authorization", "x-forwarded-user", "x-unlisted"):
        assert called_headers.get(forbidden) is None


@pytest.mark.asyncio
async def test_proxy_strips_encoding_headers_from_decoded_json_response():
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://api.weather.com/mcp",
            summary="Weather summary",
        )
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers(
        {
            "content-type": "application/json",
            "content-encoding": "gzip",
            "content-length": "9999",
            "x-upstream": "kept",
        }
    )
    mock_response.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","result":{"tools":[]}}')
    mock_stream = MagicMock()
    mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream.__aexit__ = AsyncMock(return_value=None)

    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        with patch("httpx.AsyncClient.stream", return_value=mock_stream):
            response = await client.post(
                "/weather",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
            )

    assert response.status_code == 200
    assert response.headers.get("content-encoding") is None
    assert response.headers.get("content-length") != "9999"
    assert response.headers.get("x-upstream") == "kept"
    assert response.json()["result"]["tools"] == []


@pytest.mark.asyncio
async def test_streamable_http_direct_post_accepts_json_only_clients():
    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http",
        )
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"content-type": "application/json"})
    mock_response.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":1,"result":{}}')
    mock_stream = MagicMock()
    mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream.__aexit__ = AsyncMock(return_value=None)

    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        with patch("httpx.AsyncClient.stream", return_value=mock_stream) as mock_stream_call:
            response = await client.post(
                "/huggingface",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json"},
            )
    assert response.status_code == 200
    assert mock_stream_call.call_args.kwargs["headers"]["accept"] == "application/json, text/event-stream"


@pytest.mark.asyncio
async def test_streamable_http_direct_post_rejects_missing_jsonrpc_version():
    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http",
        )
    }
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        with patch("httpx.AsyncClient.stream") as mock_stream_call:
            response = await client.post(
                "/huggingface",
                json={"id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json"},
            )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    mock_stream_call.assert_not_called()


@pytest.mark.asyncio
async def test_streamable_http_direct_post_rejects_jsonrpc_batch():
    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http",
        )
    }
    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        with patch("httpx.AsyncClient.stream") as mock_stream_call:
            response = await client.post(
                "/huggingface",
                json=[
                    {"id": 1, "method": "tools/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}},
                ],
                headers={"Accept": "application/json"},
            )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32600
    mock_stream_call.assert_not_called()


@pytest.mark.asyncio
async def test_sse_message_post_uses_upstream_message_path():
    router._configs = {
        "crawl4ai": EndpointConfig(
            path="crawl4ai",
            mode="remote",
            url="http://127.0.0.1:11235/mcp/sse",
            summary="Crawl4AI",
            transport="sse",
            headers={"Authorization": "Bearer token"},
        )
    }
    mock_response = MagicMock()
    mock_response.status_code = 200
    mock_response.headers = httpx.Headers({"content-type": "application/json"})
    mock_response.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":1,"result":{}}')
    mock_stream = MagicMock()
    mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
    mock_stream.__aexit__ = AsyncMock(return_value=None)

    async with AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://localhost") as client:
        with patch("httpx.AsyncClient.stream", return_value=mock_stream) as mock_stream_call:
            response = await client.post(
                "/crawl4ai/mcp/messages/?session_id=abc",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json"},
            )
    assert response.status_code == 200
    assert mock_stream_call.call_args.kwargs["url"] == "http://127.0.0.1:11235/mcp/messages/"
    assert mock_stream_call.call_args.kwargs["headers"]["Authorization"] == "Bearer token"
