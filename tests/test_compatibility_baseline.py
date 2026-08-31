from __future__ import annotations

from pathlib import Path
from unittest.mock import patch

import httpx
import pytest

from mcp_router.core.config_loader import EndpointConfig, load_router_config
from mcp_router.server import app, router
from tests.fixtures.mock_upstream import (
    CLIENT_CAPABILITIES_META,
    CLIENT_INFO_META,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META,
    MockMCPUpstream,
    MockMode,
)

OPERATOR_VERIFIED_MODERN_SCENARIOS = [
    ("opencode-modern-firecrawl", "firecrawl"),
    ("opencode-modern-context7", "context7"),
    ("opencode-modern-huggingface", "huggingface"),
    ("opencode-modern-github", "github"),
    ("codex-modern-governed-gh-cli", "gh-cli"),
]


@pytest.fixture(autouse=True)
def _reset_router_runtime_state():
    router._configs = {}
    router._http_client = None
    router._legacy_bridge = None
    router._metrics.denied_calls.clear()
    router._metrics.upstream_errors.clear()
    router._metrics.stream_cancellations.clear()
    yield
    router._configs = {}
    router._http_client = None
    router._legacy_bridge = None
    router._metrics.denied_calls.clear()
    router._metrics.upstream_errors.clear()
    router._metrics.stream_cancellations.clear()


def _outbound_client_factory(upstream: MockMCPUpstream):
    original_async_client = httpx.AsyncClient

    def factory(*args, **kwargs):
        kwargs["transport"] = upstream.transport
        return original_async_client(*args, **kwargs)

    return factory


def _modern_meta(client_name: str = "phase0-modern-client") -> dict[str, object]:
    return {
        PROTOCOL_VERSION_META: MODERN_PROTOCOL_VERSION,
        CLIENT_INFO_META: {"name": client_name, "version": "1"},
        CLIENT_CAPABILITIES_META: {},
    }


def _modern_request(
    request_id: int,
    method: str,
    params: dict[str, object] | None = None,
    *,
    client_name: str = "phase0-modern-client",
) -> dict[str, object]:
    request_params = dict(params or {})
    request_params["_meta"] = _modern_meta(client_name)
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": request_params,
    }


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
    config_path = Path(__file__).parent / "fixtures" / "compatibility-baseline-config.yaml"
    config = load_router_config(str(config_path))
    endpoint = next(item for item in config.endpoints if item.path == path)

    assert endpoint.mode == mode
    assert endpoint.url == url
    assert endpoint.transport == "streamable-http"
    assert endpoint.legacy_sse_bridge is None


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("scenario_id", "namespace"),
    OPERATOR_VERIFIED_MODERN_SCENARIOS,
    ids=[scenario[0] for scenario in OPERATOR_VERIFIED_MODERN_SCENARIOS],
)
async def test_operator_verified_modern_client_scenarios(scenario_id: str, namespace: str):
    router._configs = {
        namespace: EndpointConfig(
            path=namespace,
            mode="remote",
            url="http://mock-upstream.local/mcp",
            summary=f"Modern deterministic mock for {scenario_id}",
            transport="streamable-http",
        )
    }
    upstream = MockMCPUpstream("modern-stateless")
    inbound_transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=inbound_transport, base_url="http://localhost") as client:
        with patch(
            "mcp_router.server.httpx.AsyncClient",
            side_effect=_outbound_client_factory(upstream),
        ):
            first = await client.post(
                f"/{namespace}",
                json=_modern_request(
                    1,
                    "tools/call",
                    {"name": "baseline_tool", "arguments": {}},
                    client_name=scenario_id,
                ),
                headers={
                    "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "baseline_tool",
                    "Accept": "application/json",
                },
            )
            second = await client.post(
                f"/{namespace}",
                json=_modern_request(2, "tools/list", client_name=scenario_id),
                headers={
                    "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                    "Mcp-Method": "tools/list",
                    "Accept": "application/json",
                },
            )

    assert first.status_code == 200
    assert first.json()["result"]["resultType"] == "complete"
    assert first.json()["result"]["content"][0]["text"] == "baseline-ok"
    assert second.status_code == 200
    assert second.json()["result"]["resultType"] == "complete"
    tool = second.json()["result"]["tools"][0]
    assert tool["name"] == "baseline_tool"
    assert tool["inputSchema"]["type"] == "object"
    assert router._legacy_bridge is None
    assert len(upstream.requests) == 2
    assert all(request.path == "/mcp" for request in upstream.requests)

    first_request = upstream.requests[0]
    assert first_request.headers["mcp-protocol-version"] == MODERN_PROTOCOL_VERSION
    assert first_request.headers["mcp-method"] == "tools/call"
    assert first_request.headers["mcp-name"] == "baseline_tool"
    assert first_request.headers["accept"] == "application/json, text/event-stream"
    first_body = first_request.json_body
    assert isinstance(first_body, dict)
    first_params = first_body["params"]
    assert isinstance(first_params, dict)
    first_meta = first_params["_meta"]
    assert isinstance(first_meta, dict)
    assert first_meta[PROTOCOL_VERSION_META] == MODERN_PROTOCOL_VERSION
    assert first_meta[CLIENT_CAPABILITIES_META] == {}


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "missing_meta_key",
    [PROTOCOL_VERSION_META, CLIENT_CAPABILITIES_META],
)
async def test_modern_fixture_rejects_missing_required_per_request_metadata(missing_meta_key: str):
    upstream = MockMCPUpstream("modern-stateless")
    request = _modern_request(1, "tools/list")
    params = request["params"]
    assert isinstance(params, dict)
    meta = params["_meta"]
    assert isinstance(meta, dict)
    del meta[missing_meta_key]

    async with httpx.AsyncClient(transport=upstream.transport, base_url="http://mock-upstream.local") as client:
        response = await client.post(
            "/mcp",
            json=request,
            headers={
                "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                "Mcp-Method": "tools/list",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32602


@pytest.mark.asyncio
async def test_modern_fixture_rejects_protocol_header_body_mismatch():
    upstream = MockMCPUpstream("modern-stateless")
    request = _modern_request(1, "tools/list")
    params = request["params"]
    assert isinstance(params, dict)
    meta = params["_meta"]
    assert isinstance(meta, dict)
    meta[PROTOCOL_VERSION_META] = "2099-01-01"

    async with httpx.AsyncClient(transport=upstream.transport, base_url="http://mock-upstream.local") as client:
        response = await client.post(
            "/mcp",
            json=request,
            headers={
                "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                "Mcp-Method": "tools/list",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32020


@pytest.mark.asyncio
async def test_modern_fixture_rejects_matching_unsupported_protocol_version():
    upstream = MockMCPUpstream("modern-stateless")
    unsupported_version = "2099-01-01"
    request = _modern_request(1, "tools/list")
    params = request["params"]
    assert isinstance(params, dict)
    meta = params["_meta"]
    assert isinstance(meta, dict)
    meta[PROTOCOL_VERSION_META] = unsupported_version

    async with httpx.AsyncClient(transport=upstream.transport, base_url="http://mock-upstream.local") as client:
        response = await client.post(
            "/mcp",
            json=request,
            headers={
                "MCP-Protocol-Version": unsupported_version,
                "Mcp-Method": "tools/list",
            },
        )

    assert response.status_code == 400
    assert response.json()["error"]["code"] == -32022
    assert response.json()["error"]["data"] == {"supportedVersions": [MODERN_PROTOCOL_VERSION]}


@pytest.mark.asyncio
async def test_modern_fixture_uses_resource_uri_for_mcp_name():
    upstream = MockMCPUpstream("modern-stateless")
    uri = "file:///phase0/resource.txt"
    request = _modern_request(1, "resources/read", {"uri": uri})

    async with httpx.AsyncClient(transport=upstream.transport, base_url="http://mock-upstream.local") as client:
        accepted = await client.post(
            "/mcp",
            json=request,
            headers={
                "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                "Mcp-Method": "resources/read",
                "Mcp-Name": uri,
            },
        )
        rejected = await client.post(
            "/mcp",
            json=request,
            headers={
                "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                "Mcp-Method": "resources/read",
                "Mcp-Name": "not-the-uri",
            },
        )

    assert accepted.status_code == 200
    assert accepted.json()["result"]["resultType"] == "complete"
    assert rejected.status_code == 400
    assert rejected.json()["error"]["code"] == -32020


@pytest.mark.asyncio
async def test_modern_fixture_does_not_use_legacy_session_header_for_protocol_state():
    upstream = MockMCPUpstream("modern-stateless")

    async with httpx.AsyncClient(transport=upstream.transport, base_url="http://mock-upstream.local") as client:
        response = await client.post(
            "/mcp",
            json=_modern_request(1, "tools/list"),
            headers={
                "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                "Mcp-Method": "tools/list",
                "Mcp-Session-Id": "legacy-header-must-not-create-modern-session-state",
            },
        )

    assert response.status_code == 200
    assert response.json()["result"]["resultType"] == "complete"


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("mode", "expected_status", "expected_error"),
    [
        ("malformed-json", 502, "Upstream returned malformed JSON"),
        ("invalid-utf8-json", 502, "Upstream returned malformed JSON"),
        ("invalid-json-constant", 502, "Upstream returned malformed JSON"),
        ("http-failure", 503, "deterministic upstream failure"),
        ("transport-failure", 502, "Upstream proxy request failed"),
    ],
    ids=[
        "malformed-json",
        "invalid-utf8-json",
        "invalid-json-constant",
        "http-503",
        "transport-failure",
    ],
)
async def test_negative_upstream_fixtures_fail_closed_and_are_observable(
    mode: MockMode,
    expected_status: int,
    expected_error: str,
):
    router._configs = {
        "negative": EndpointConfig(
            path="negative",
            mode="remote",
            url="http://mock-upstream.local/mcp",
            summary=f"Deterministic negative fixture: {mode}",
            transport="streamable-http",
        )
    }
    upstream = MockMCPUpstream(mode)
    inbound_transport = httpx.ASGITransport(app=app)

    async with httpx.AsyncClient(transport=inbound_transport, base_url="http://localhost") as client:
        with patch(
            "mcp_router.server.httpx.AsyncClient",
            side_effect=_outbound_client_factory(upstream),
        ):
            response = await client.post(
                "/negative",
                json=_modern_request(1, "tools/list"),
                headers={
                    "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                    "Mcp-Method": "tools/list",
                    "Accept": "application/json",
                },
            )
            metrics = (await client.get("/metrics")).json()["endpoints"][0]

    assert response.status_code == expected_status
    if mode == "http-failure":
        assert response.json()["error"]["message"] == expected_error
    else:
        assert response.json()["error"] == expected_error
    assert metrics["upstream_errors_total"] == 1
    assert len(upstream.requests) == 1


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

    async with httpx.AsyncClient(transport=inbound_transport, base_url="http://localhost") as client:
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
    assert router._legacy_bridge is None
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

    async with httpx.AsyncClient(transport=inbound_transport, base_url="http://localhost") as client:
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
