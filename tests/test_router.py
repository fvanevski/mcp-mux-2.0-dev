import asyncio
import json
import os
import sys
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# Add project root to path
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from mcp_router.core.config_loader import (
    EndpointConfig,
    ManagedEndpointConfig,
    RouterConfig,
    expand_env_vars,
)
from mcp_router.core.process_manager import ProcessManager
from mcp_router.server import BridgeSession, app, filter_tools_response, router

# --- Config Loader Tests ---

def test_valid_config():
    data = {
        "endpoints": [
            {
                "path": "weather",
                "mode": "remote",
                "url": "http://api.weather.com",
                "summary": "Weather API"
            },
            {
                "path": "files",
                "mode": "managed_cli",
                "argv": ["uvx", "mcp-server-filesystem"],
                "url": "http://localhost:8011/mcp",
                "summary": "File tools"
            }
        ]
    }
    cfg = RouterConfig.model_validate(data)
    assert len(cfg.endpoints) == 2
    assert cfg.endpoints[0].path == "weather"
    assert cfg.endpoints[1].mode == "managed_cli"
    assert cfg.endpoints[1].url == "http://localhost:8011/mcp"

def test_config_port_collision():
    data = {
        "endpoints": [
            {
                "path": "files1",
                "mode": "managed_cli",
                "argv": ["uvx", "mcp-server-filesystem"],
                "url": "http://localhost:8011/mcp",
                "summary": "Files 1"
            },
            {
                "path": "files2",
                "mode": "managed_cli",
                "argv": ["uvx", "mcp-server-filesystem"],
                "url": "http://localhost:8011/mcp",
                "summary": "Files 2"
            }
        ]
    }
    with pytest.raises(ValueError, match="Duplicate port detected"):
        RouterConfig.model_validate(data)

def test_config_duplicate_path():
    data = {
        "endpoints": [
            {
                "path": "weather",
                "mode": "remote",
                "url": "http://api1.weather.com",
                "summary": "Weather 1"
            },
            {
                "path": "weather",
                "mode": "remote",
                "url": "http://api2.weather.com",
                "summary": "Weather 2"
            }
        ]
    }
    with pytest.raises(ValueError, match="Duplicate path detected"):
        RouterConfig.model_validate(data)

def test_config_missing_remote_url():
    data = {
        "endpoints": [
            {
                "path": "weather",
                "mode": "remote",
                "summary": "Missing URL"
            }
        ]
    }
    with pytest.raises(ValueError):
        RouterConfig.model_validate(data)

def test_config_missing_managed_cli_url():
    data = {
        "endpoints": [
            {
                "path": "files",
                "mode": "managed_cli",
                "argv": ["uvx"],
                "summary": "Missing URL"
            }
        ]
    }
    with pytest.raises(ValueError):
        RouterConfig.model_validate(data)

def test_config_expands_environment_variables(monkeypatch):
    monkeypatch.setenv("TEST_HF_TOKEN", "hf_test_token")
    data = {
        "endpoints": [
            {
                "path": "huggingface",
                "mode": "remote",
                "url": "https://huggingface.co/mcp",
                "summary": "HuggingFace API",
                "headers": {
                    "Authorization": "Bearer ${TEST_HF_TOKEN}",
                    "X-Default": "${MISSING_HEADER:-fallback}"
                }
            }
        ]
    }

    expanded = expand_env_vars(data)
    cfg = RouterConfig.model_validate(expanded)

    assert cfg.endpoints[0].headers == {
        "Authorization": "Bearer hf_test_token",
        "X-Default": "fallback"
    }

def test_config_env_expansion_requires_missing_variables(monkeypatch):
    monkeypatch.delenv("MISSING_HF_TOKEN", raising=False)

    with pytest.raises(ValueError, match="MISSING_HF_TOKEN"):
        expand_env_vars({"headers": {"Authorization": "Bearer ${MISSING_HF_TOKEN}"}})

def test_config_omits_empty_authorization_header(monkeypatch):
    monkeypatch.delenv("OPTIONAL_HF_TOKEN", raising=False)
    data = {
        "endpoints": [
            {
                "path": "huggingface",
                "mode": "remote",
                "url": "https://huggingface.co/mcp",
                "summary": "HuggingFace API",
                "headers": {
                    "Authorization": "Bearer ${OPTIONAL_HF_TOKEN:-}"
                }
            }
        ]
    }

    cfg = RouterConfig.model_validate(expand_env_vars(data))

    assert cfg.endpoints[0].headers is None

def test_config_normalizes_duplicate_bearer_authorization(monkeypatch):
    monkeypatch.setenv("HF_TOKEN_WITH_SCHEME", "Bearer hf_test_token")
    data = {
        "endpoints": [
            {
                "path": "huggingface",
                "mode": "remote",
                "url": "https://huggingface.co/mcp",
                "summary": "HuggingFace API",
                "headers": {
                    "Authorization": "Bearer ${HF_TOKEN_WITH_SCHEME}"
                }
            }
        ]
    }

    cfg = RouterConfig.model_validate(expand_env_vars(data))

    assert cfg.endpoints[0].headers == {"Authorization": "Bearer hf_test_token"}

# --- Process Manager Tests ---

@pytest.mark.asyncio
async def test_wait_for_port_caps_sleep_at_readiness_deadline(monkeypatch):
    pm = ProcessManager()
    now = 0.0
    sleep_calls: list[float] = []

    class FakeLoop:
        def time(self) -> float:
            return now

    async def fail_connection(host: str, port: int):
        raise ConnectionRefusedError

    async def advance_time(delay: float):
        nonlocal now
        sleep_calls.append(delay)
        now += delay

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr(asyncio, "open_connection", fail_connection)
    monkeypatch.setattr(asyncio, "sleep", advance_time)

    assert await pm._wait_for_port(3033, timeout=1.0, interval=30.0) is False
    assert sleep_calls == [1.0]
    assert now == 1.0


@pytest.mark.asyncio
async def test_wait_for_port_bounds_connection_attempt_by_remaining_deadline(monkeypatch):
    pm = ProcessManager()
    wait_for_timeouts: list[float] = []

    class FakeLoop:
        def time(self) -> float:
            return 0.0

    async def pending_connection(host: str, port: int):
        await asyncio.Event().wait()

    async def force_timeout(awaitable, *, timeout: float):
        wait_for_timeouts.append(timeout)
        awaitable.close()
        raise TimeoutError

    monkeypatch.setattr(asyncio, "get_running_loop", lambda: FakeLoop())
    monkeypatch.setattr(asyncio, "open_connection", pending_connection)
    monkeypatch.setattr(asyncio, "wait_for", force_timeout)

    assert await pm._wait_for_port(3033, timeout=1.0, interval=30.0) is False
    assert wait_for_timeouts == [1.0]


@pytest.mark.asyncio
async def test_process_manager_lifecycle():
    pm = ProcessManager()
    # Reset singleton internal state for testing
    pm._processes.clear()
    pm._log_tasks.clear()

    cfg = ManagedEndpointConfig(
        path="mock-mcp",
        mode="managed_cli",
        argv=["python", "-m", "http.server", "8099"],
        url="http://localhost:8099/mcp",
        summary="Mock python server"
    )

    # Mock safe argv execution and readiness.
    mock_proc = AsyncMock()
    mock_proc.pid = 99999
    mock_proc.returncode = None
    mock_proc.stdout = AsyncMock()
    mock_proc.stderr = AsyncMock()

    with patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec, \
         patch.object(pm, "_wait_for_port", return_value=True) as mock_wait:
        
        target_url = await pm.start_managed_server(cfg)
        
        assert target_url == "http://localhost:8099/mcp"
        assert mock_exec.call_args.args == ("python", "-m", "http.server", "8099")
        mock_wait.assert_called_once_with(8099, host="localhost", timeout=15.0, interval=0.2)
        assert "mock-mcp" in pm._processes

        # Test stop
        with patch("os.killpg") as mock_killpg, patch("os.getpgid", return_value=123):
            await pm.stop_managed_server("mock-mcp")
            mock_killpg.assert_called_once_with(123, 15)  # signal.SIGTERM is 15
            assert "mock-mcp" not in pm._processes

# --- Routes & App Tests ---

@pytest.mark.asyncio
async def test_summary_route():
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://weather/mcp",
            summary="Weather summary"
        )
    }

    import httpx
    from httpx import AsyncClient
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/summary")

    assert response.status_code == 200
    
    data = response.json()
    assert "endpoints" in data
    assert len(data["endpoints"]) == 1
    assert data["endpoints"][0]["path"] == "weather"
    assert data["endpoints"][0]["mode"] == "remote"
    assert data["endpoints"][0]["summary"] == "Weather summary"

@pytest.mark.asyncio
async def test_not_found_route():
    import httpx
    from httpx import AsyncClient
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        response = await client.get("/nonexistent")

    assert response.status_code == 404
    assert "configured" in response.json()["error"]


@pytest.mark.asyncio
async def test_streamable_http_plain_get_returns_json_not_upstream_html():
    from starlette.requests import Request

    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http"
        )
    }
    router.active_sessions.clear()

    mock_req = MagicMock(spec=Request)
    mock_req.method = "GET"
    mock_req.path_params = {"path_prefix": "huggingface"}
    mock_req.url.path = "/huggingface"
    mock_req.headers = {"accept": "*/*"}
    mock_req.query_params = {}

    with patch("httpx.AsyncClient.stream") as mock_stream:
        response = await router.catch_all_proxy(mock_req)

    assert response.status_code == 405
    assert response.media_type == "application/json"
    assert b"Streamable HTTP" in response.body
    assert router.active_sessions == {}
    mock_stream.assert_not_called()


@pytest.mark.asyncio
async def test_streamable_http_sse_get_preserves_upstream_streamable_http():
    from starlette.requests import Request

    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http"
        )
    }
    router.active_sessions.clear()

    mock_req = MagicMock(spec=Request)
    mock_req.method = "GET"
    mock_req.path_params = {"path_prefix": "huggingface"}
    mock_req.url.path = "/huggingface"
    mock_req.headers = {"accept": "text/event-stream"}
    mock_req.query_params = {}
    mock_req.body = AsyncMock(return_value=b"")

    import httpx
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
    assert router.active_sessions == {}
    mock_stream_call.assert_called_once()
    assert mock_stream_call.call_args[1]["method"] == "GET"
    assert mock_stream_call.call_args[1]["url"] == "https://huggingface.co/mcp"


@pytest.mark.asyncio
async def test_streamable_http_sse_get_opens_local_sse_bridge_when_enabled():
    from starlette.requests import Request

    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http",
            legacy_sse_bridge=True
        )
    }
    router.active_sessions.clear()

    mock_req = MagicMock(spec=Request)
    mock_req.method = "GET"
    mock_req.path_params = {"path_prefix": "huggingface"}
    mock_req.url.path = "/huggingface"
    mock_req.headers = {"accept": "text/event-stream"}
    mock_req.query_params = {}

    with patch("httpx.AsyncClient.stream") as mock_stream:
        response = await router.catch_all_proxy(mock_req)

    assert response.status_code == 200
    assert response.media_type == "text/event-stream"
    assert len(router.active_sessions) == 1
    session = next(iter(router.active_sessions.values()))
    assert session.path_prefix == "huggingface"
    mock_stream.assert_not_called()


@pytest.mark.asyncio
async def test_streamable_http_bridge():
    session_id = "local-session-123"
    queue = asyncio.Queue()
    router.active_sessions.clear()
    router.active_sessions[session_id] = BridgeSession(path_prefix="weather", queue=queue)

    stream = router.local_sse_generator(session_id, queue, "weather")
    try:
        endpoint_event = (await anext(stream)).decode("utf-8")
        assert "event: endpoint" in endpoint_event
        assert f"data: /weather?session_id={session_id}" in endpoint_event

        await queue.put("event: message")
        await queue.put('data: {"result":"cloudy"}')

        event_line = (await anext(stream)).decode("utf-8").strip()
        data_line = (await anext(stream)).decode("utf-8").strip()
        assert event_line == "event: message"
        assert data_line == 'data: {"result":"cloudy"}'
    finally:
        await stream.aclose()

    assert session_id not in router.active_sessions


# --- Tool Filtering Tests ---

def test_endpoint_config_allowed_denied_validation():
    # Both provided -> denied_tools is cleared to None (precedence to allowed)
    cfg = EndpointConfig(
        path="test",
        mode="remote",
        url="http://localhost/mcp",
        summary="Test",
        allowed_tools=["toolA"],
        denied_tools=["toolB"]
    )
    assert cfg.allowed_tools == ["toolA"]
    assert cfg.denied_tools is None

    # Only allowed provided -> preserved
    cfg_allow = EndpointConfig(
        path="test",
        mode="remote",
        url="http://localhost/mcp",
        summary="Test",
        allowed_tools=["toolA"]
    )
    assert cfg_allow.allowed_tools == ["toolA"]
    assert cfg_allow.denied_tools is None

    # Only denied provided -> preserved
    cfg_deny = EndpointConfig(
        path="test",
        mode="remote",
        url="http://localhost/mcp",
        summary="Test",
        denied_tools=["toolB"]
    )
    assert cfg_deny.allowed_tools is None
    assert cfg_deny.denied_tools == ["toolB"]

def test_filter_tools_response_sse():
    allowed = ["toolA"]
    line = 'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"toolA"},{"name":"toolB"}]}}'
    filtered = filter_tools_response(line, allowed, None)
    assert filtered.startswith("data: ")
    import json
    data = json.loads(filtered[6:])
    assert len(data["result"]["tools"]) == 1
    assert data["result"]["tools"][0]["name"] == "toolA"

def test_filter_tools_response_json():
    denied = ["toolA"]
    body = '{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"toolA"},{"name":"toolB"}]}}'
    filtered = filter_tools_response(body, None, denied)
    import json
    data = json.loads(filtered)
    assert len(data["result"]["tools"]) == 1
    assert data["result"]["tools"][0]["name"] == "toolB"


def test_config_with_headers():
    data = {
        "endpoints": [
            {
                "path": "weather",
                "mode": "remote",
                "url": "http://api.weather.com",
                "summary": "Weather API",
                "headers": {
                    "Authorization": "Bearer mytoken",
                    "X-Custom-Header": "value"
                }
            }
        ]
    }
    cfg = RouterConfig.model_validate(data)
    assert len(cfg.endpoints) == 1
    assert cfg.endpoints[0].headers == {
        "Authorization": "Bearer mytoken",
        "X-Custom-Header": "value"
    }


@pytest.mark.asyncio
async def test_proxy_headers_forwarding():
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://api.weather.com/mcp",
            summary="Weather summary",
            headers={"X-Custom-Auth": "secret-token", "X-Override": "router-value"}
        )
    }

    import httpx
    from httpx import AsyncClient
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_response.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","result":{"tools":[]}}')
        
        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream.__aexit__ = AsyncMock(return_value=None)
        
        with patch("httpx.AsyncClient.stream", return_value=mock_stream) as mock_stream_call:
            response = await client.post(
                "/weather",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={"X-Override": "client-value", "X-Client-Header": "only-client"}
            )
            assert response.status_code == 200
            
            mock_stream_call.assert_called_once()
            called_kwargs = mock_stream_call.call_args[1]
            called_headers = called_kwargs.get("headers")
            
            # Custom auth header from config must be present
            assert called_headers.get("x-custom-auth") == "secret-token" or called_headers.get("X-Custom-Auth") == "secret-token"
            # Override header must have the value from the config
            assert called_headers.get("x-override") == "router-value" or called_headers.get("X-Override") == "router-value"
            # Client header must still be present
            assert called_headers.get("x-client-header") == "only-client" or called_headers.get("X-Client-Header") == "only-client"


@pytest.mark.asyncio
async def test_proxy_strips_encoding_headers_from_decoded_json_response():
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://api.weather.com/mcp",
            summary="Weather summary"
        )
    }

    import httpx
    from httpx import AsyncClient
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({
            "content-type": "application/json",
            "content-encoding": "gzip",
            "content-length": "9999",
            "x-upstream": "kept"
        })
        mock_response.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","result":{"tools":[]}}')

        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

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
            transport="streamable-http"
        )
    }

    import httpx
    from httpx import AsyncClient
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_response.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":1,"result":{}}')

        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient.stream", return_value=mock_stream) as mock_stream_call:
            response = await client.post(
                "/huggingface",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json"}
            )

    assert response.status_code == 200
    called_headers = mock_stream_call.call_args[1]["headers"]
    assert called_headers["accept"] == "application/json, text/event-stream"


@pytest.mark.asyncio
async def test_streamable_http_direct_post_rejects_missing_jsonrpc_version():
    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http"
        )
    }

    import httpx
    from httpx import AsyncClient
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_response.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":1,"result":{}}')

        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient.stream", return_value=mock_stream) as mock_stream_call:
            response = await client.post(
                "/huggingface",
                json={"id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json"}
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
            transport="streamable-http"
        )
    }

    import httpx
    from httpx import AsyncClient
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_response.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":1,"result":{}}')

        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient.stream", return_value=mock_stream) as mock_stream_call:
            response = await client.post(
                "/huggingface",
                json=[
                    {"id": 1, "method": "tools/list", "params": {}},
                    {"jsonrpc": "2.0", "id": 2, "method": "prompts/list", "params": {}},
                    {"not": "rpc"}
                ],
                headers={"Accept": "application/json"}
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
            headers={"Authorization": "Bearer token"}
        )
    }

    import httpx
    from httpx import AsyncClient
    transport = httpx.ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        mock_response = MagicMock()
        mock_response.status_code = 200
        mock_response.headers = httpx.Headers({"content-type": "application/json"})
        mock_response.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":1,"result":{}}')

        mock_stream = MagicMock()
        mock_stream.__aenter__ = AsyncMock(return_value=mock_response)
        mock_stream.__aexit__ = AsyncMock(return_value=None)

        with patch("httpx.AsyncClient.stream", return_value=mock_stream) as mock_stream_call:
            response = await client.post(
                "/crawl4ai/mcp/messages/?session_id=abc",
                json={"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}},
                headers={"Accept": "application/json"}
            )

    assert response.status_code == 200
    assert mock_stream_call.call_args[1]["url"] == "http://127.0.0.1:11235/mcp/messages/"
    assert mock_stream_call.call_args[1]["headers"]["Authorization"] == "Bearer token"


@pytest.mark.asyncio
async def test_streamable_http_direct_response_json():
    import httpx
    from starlette.requests import Request
    # Configure endpoint
    router._configs = {
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http"
        )
    }
    router.active_sessions.clear()

    # Create a local session queue to simulate active SSE connection
    local_session_id = "local-session-123"
    queue = asyncio.Queue()
    router.active_sessions[local_session_id] = BridgeSession(path_prefix="huggingface", queue=queue)

    # 1. Test POST request for 'initialize'
    # This should:
    # - Call the remote server WITHOUT Mcp-Session-Id header (since it's not mapped yet)
    # - Set Accept header to "application/json, text/event-stream"
    # - Capture and map the remote session ID returned in response headers
    # - Wrap direct JSON response into SSE message event on the queue
    mock_init_resp = MagicMock()
    mock_init_resp.status_code = 200
    mock_init_resp.headers = httpx.Headers({
        "content-type": "application/json",
        "mcp-session-id": "remote-session-456"
    })
    mock_init_resp.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","result":{"protocolVersion":"2024-11-05"},"id":1}')
    
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

    with patch("httpx.AsyncClient.stream", return_value=mock_init_stream) as mock_stream_call:
        response = await router.catch_all_proxy(mock_req_init)
        assert response.status_code == 202
        assert response.body == b"Accepted"
        
        # Verify call arguments
        mock_stream_call.assert_called_once()
        called_kwargs = mock_stream_call.call_args[1]
        assert "Mcp-Session-Id" not in called_kwargs["headers"]
        assert called_kwargs["headers"].get("accept") == "application/json, text/event-stream"
        
        # Verify remote session ID was mapped
        assert router.active_sessions[local_session_id].remote_session_id == "remote-session-456"
        
        # Verify the wrapped SSE events in the queue
        e1 = await queue.get()
        e2 = await queue.get()
        e3 = await queue.get()
        assert e1 == "event: message"
        assert e2 == 'data: {"jsonrpc":"2.0","result":{"protocolVersion":"2024-11-05"},"id":1}'
        assert e3 == ""
        assert queue.empty()

    # 2. Test subsequent POST request for 'tools/list'
    # This should:
    # - Call the remote server WITH Mcp-Session-Id header (using the mapped session ID)
    # - Set Accept header to "application/json, text/event-stream"
    # - Wrap direct JSON response into SSE message event on the queue
    mock_tools_resp = MagicMock()
    mock_tools_resp.status_code = 200
    mock_tools_resp.headers = httpx.Headers({"content-type": "application/json"})
    mock_tools_resp.aread = AsyncMock(return_value=b'{"jsonrpc":"2.0","result":{"tools":[]},"id":2}')
    
    mock_tools_stream = MagicMock()
    mock_tools_stream.__aenter__ = AsyncMock(return_value=mock_tools_resp)
    mock_tools_stream.__aexit__ = AsyncMock(return_value=None)

    mock_req_tools = MagicMock(spec=Request)
    mock_req_tools.method = "POST"
    mock_req_tools.path_params = {"path_prefix": "huggingface"}
    mock_req_tools.url.path = "/huggingface"
    mock_req_tools.headers = {"accept": "application/json", "content-type": "application/json"}
    mock_req_tools.query_params = {"session_id": local_session_id}
    mock_req_tools.body = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":2,"method":"tools/list"}')

    with patch("httpx.AsyncClient.stream", return_value=mock_tools_stream) as mock_stream_call2:
        response2 = await router.catch_all_proxy(mock_req_tools)
        assert response2.status_code == 202
        assert response2.body == b"Accepted"
        
        # Verify call arguments
        mock_stream_call2.assert_called_once()
        called_kwargs2 = mock_stream_call2.call_args[1]
        assert called_kwargs2["headers"].get("Mcp-Session-Id") == "remote-session-456"
        assert called_kwargs2["headers"].get("accept") == "application/json, text/event-stream"
        
        # Verify the wrapped SSE events in the queue
        e4 = await queue.get()
        e5 = await queue.get()
        e6 = await queue.get()
        assert e4 == "event: message"
        assert e5 == 'data: {"jsonrpc":"2.0","result":{"tools":[]},"id":2}'
        assert e6 == ""
        assert queue.empty()


@pytest.mark.asyncio
async def test_streamable_http_rejects_session_for_different_endpoint():
    from starlette.requests import Request

    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://api.weather.com/mcp",
            summary="Weather summary",
            transport="streamable-http"
        ),
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http"
        )
    }
    router.active_sessions.clear()
    local_session_id = "weather-session"
    router.active_sessions[local_session_id] = BridgeSession(
        path_prefix="weather",
        queue=asyncio.Queue(),
        remote_session_id="remote-weather-session"
    )

    mock_req = MagicMock(spec=Request)
    mock_req.method = "POST"
    mock_req.path_params = {"path_prefix": "huggingface"}
    mock_req.url.path = "/huggingface"
    mock_req.headers = {"accept": "application/json", "content-type": "application/json"}
    mock_req.query_params = {"session_id": local_session_id}
    mock_req.body = AsyncMock(return_value=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}')

    with patch("httpx.AsyncClient.stream") as mock_stream:
        response = await router.catch_all_proxy(mock_req)

    assert response.status_code == 409
    assert b"different endpoint" in response.body
    mock_stream.assert_not_called()


@pytest.mark.asyncio
async def test_apply_configuration_drops_sessions_for_removed_or_changed_endpoint():
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://api.weather.com/mcp",
            summary="Weather summary",
            transport="streamable-http"
        ),
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http"
        ),
        "files": EndpointConfig(
            path="files",
            mode="remote",
            url="http://files.example.com/mcp",
            summary="Files remote server",
            transport="streamable-http"
        )
    }
    router.active_sessions.clear()
    router.active_sessions["weather-session"] = BridgeSession(
        path_prefix="weather",
        queue=asyncio.Queue(),
        remote_session_id="remote-weather-session"
    )
    router.active_sessions["hf-session"] = BridgeSession(
        path_prefix="huggingface",
        queue=asyncio.Queue(),
        remote_session_id="remote-hf-session"
    )
    router.active_sessions["files-session"] = BridgeSession(
        path_prefix="files",
        queue=asyncio.Queue(),
        remote_session_id="remote-files-session"
    )

    new_config = RouterConfig.model_validate({
        "endpoints": [
            {
                "path": "weather",
                "mode": "remote",
                "url": "http://api.weather.com/v2/mcp",
                "summary": "Updated weather summary"
            },
            {
                "path": "huggingface",
                "mode": "remote",
                "url": "https://huggingface.co/mcp",
                "summary": "HuggingFace remote server"
            }
        ]
    })

    router.apply_configuration(new_config)
    await asyncio.sleep(0)

    assert "weather-session" not in router.active_sessions
    assert "files-session" not in router.active_sessions
    assert "hf-session" in router.active_sessions
