from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from mcp_router.core.config_loader import EndpointConfig, load_router_config
from mcp_router.server import app, router
from tests.fixtures.mock_upstream import MockMCPUpstream


@pytest.fixture(autouse=True)
def _reset_router_runtime_state():
    router._configs = {}
    router.active_sessions.clear()
    router.active_connections.clear()
    router.last_activity.clear()
    router.locks.clear()
    yield
    router._configs = {}
    router.active_sessions.clear()
    router.active_connections.clear()
    router.last_activity.clear()
    router.locks.clear()


def _outbound_client_factory(upstream: MockMCPUpstream):
    original_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = upstream.transport
        return original_async_client(*args, **kwargs)

    return factory


@pytest.mark.parametrize(
    ("path", "mode", "url"),
    [
        ("web-search", "remote", "https://mcp.garion.us/mcp"),
        ("firecrawl", "managed_cli", "http://localhost:3033/mcp"),
        ("huggingface", "remote", "https://huggingface.co/mcp"),
        ("context7", "remote", "https://mcp.context7.com/mcp"),
    ],
    ids=[
        "modern-remote-web-search",
        "modern-managed-firecrawl",
        "modern-remote-huggingface",
        "modern-remote-context7",
    ],
)
def test_configured_upstreams_are_frozen_as_streamable_http(path: str, mode: str, url: str):
    config_path = Path(__file__).parents[1] / "mcp_router" / "config.yaml"
    config = load_router_config(str(config_path))
    endpoint = next(item for item in config.endpoints if item.path == path)

    assert endpoint.mode == mode
    assert endpoint.url == url
    assert endpoint.transport == "streamable-http"
    assert endpoint.legacy_sse_bridge is False


@pytest.mark.asyncio
async def test_modern_stateless_streamable_http_baseline():
    router._configs = {
        "modern": EndpointConfig(
            path="modern",
            mode="remote",
            url="http://mock-upstream.local/mcp",
            summary="Modern deterministic mock",
            transport="streamable-http",
        )
    }
    upstream = MockMCPUpstream("modern-stateless")
    inbound_transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=inbound_transport, base_url="http://mux.local") as client:
        with patch(
            "mcp_router.server.httpx.AsyncClient",
            side_effect=_outbound_client_factory(upstream),
        ):
            first = await client.post(
                "/modern",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "tools/call",
                    "params": {"name": "baseline_tool", "arguments": {}},
                    "_meta": {
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "phase0-modern-client",
                            "version": "1",
                        }
                    },
                },
                headers={
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "baseline_tool",
                    "Accept": "application/json",
                },
            )
            second = await client.post(
                "/modern",
                json={
                    "jsonrpc": "2.0",
                    "id": 2,
                    "method": "tools/list",
                    "params": {},
                    "_meta": {
                        "io.modelcontextprotocol/clientInfo": {
                            "name": "phase0-modern-client",
                            "version": "1",
                        }
                    },
                },
                headers={
                    "MCP-Protocol-Version": "2026-07-28",
                    "Mcp-Method": "tools/list",
                    "Accept": "application/json",
                },
            )

    assert first.status_code == 200
    assert first.json()["result"]["content"][0]["text"] == "baseline-ok"
    assert second.status_code == 200
    assert second.json()["result"]["tools"][0]["name"] == "baseline_tool"
    assert router.active_sessions == {}
    assert len(upstream.requests) == 2
    assert all(request.path == "/mcp" for request in upstream.requests)
    assert all("mcp-session-id" not in request.headers for request in upstream.requests)
    assert upstream.requests[0].headers["mcp-protocol-version"] == "2026-07-28"
    assert upstream.requests[0].headers["mcp-method"] == "tools/call"
    assert upstream.requests[0].headers["mcp-name"] == "baseline_tool"
    assert upstream.requests[0].headers["accept"] == "application/json, text/event-stream"


@pytest.mark.asyncio
async def test_legacy_sessionful_streamable_http_baseline():
    router._configs = {
        "legacy-sessionful": EndpointConfig(
            path="legacy-sessionful",
            mode="remote",
            url="http://mock-upstream.local/mcp",
            summary="Legacy sessionful deterministic mock",
            transport="streamable-http",
        )
    }
    upstream = MockMCPUpstream("legacy-sessionful")
    inbound_transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=inbound_transport, base_url="http://mux.local") as client:
        with patch(
            "mcp_router.server.httpx.AsyncClient",
            side_effect=_outbound_client_factory(upstream),
        ):
            initialize = await client.post(
                "/legacy-sessionful",
                json={
                    "jsonrpc": "2.0",
                    "id": 1,
                    "method": "initialize",
                    "params": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {},
                        "clientInfo": {"name": "phase0-legacy-client", "version": "1"},
                    },
                },
                headers={"Accept": "application/json"},
            )
            session_id = initialize.headers["mcp-session-id"]
            tools = await client.post(
                "/legacy-sessionful",
                json={"jsonrpc": "2.0", "id": 2, "method": "tools/list", "params": {}},
                headers={
                    "Accept": "application/json",
                    "Mcp-Session-Id": session_id,
                },
            )

    assert initialize.status_code == 200
    assert session_id == upstream.legacy_session_id
    assert tools.status_code == 200
    assert tools.json()["result"]["tools"][0]["name"] == "legacy_tool"
    assert router.active_sessions == {}
    assert upstream.requests[1].headers["mcp-session-id"] == upstream.legacy_session_id


@pytest.mark.asyncio
async def test_legacy_http_sse_baseline():
    router._configs = {
        "legacy-sse": EndpointConfig(
            path="legacy-sse",
            mode="remote",
            url="http://mock-upstream.local/mcp/sse",
            summary="Legacy HTTP+SSE deterministic mock",
            transport="sse",
        )
    }
    upstream = MockMCPUpstream("legacy-http-sse")
    inbound_transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=inbound_transport, base_url="http://mux.local") as client:
        with patch(
            "mcp_router.server.httpx.AsyncClient",
            side_effect=_outbound_client_factory(upstream),
        ):
            stream = await client.get(
                "/legacy-sse",
                headers={"Accept": "text/event-stream"},
            )
            post = await client.post(
                "/legacy-sse/mcp/messages/?session_id=legacy-upstream-001",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list", "params": {}},
                headers={"Accept": "application/json"},
            )

    assert stream.status_code == 200
    assert stream.headers["content-type"].startswith("text/event-stream")
    assert "event: endpoint" in stream.text
    assert "data: /legacy-sse/mcp/messages/?session_id=legacy-upstream-001" in stream.text
    assert post.status_code == 202
    assert post.json() == {"accepted": True}
    assert upstream.requests[0].method == "GET"
    assert upstream.requests[0].path == "/mcp/sse"
    assert upstream.requests[1].method == "POST"
    assert upstream.requests[1].path == "/mcp/messages/"
    assert upstream.requests[1].query == "session_id=legacy-upstream-001"
