from __future__ import annotations

import asyncio
import json
import subprocess
import sys
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import StreamingResponse

from mcp_router.compat.legacy_sse import BridgeSession, LegacySSEBridge
from mcp_router.core.config_loader import Endpoint, RouterConfig
from mcp_router.server import MCPRouter


def _endpoint(
    *,
    path: str = "legacy",
    bridge: dict[str, object] | None = None,
) -> Endpoint:
    data: dict[str, object] = {
        "path": path,
        "mode": "remote",
        "url": f"https://{path}.example.test/mcp",
        "summary": f"{path} fixture",
        "transport": "streamable-http",
    }
    if bridge is not None:
        data["legacy_sse_bridge"] = bridge
    return RouterConfig.model_validate({"endpoints": [data]}).endpoints[0]


def _request(
    method: str,
    path_prefix: str,
    *,
    headers: dict[str, str] | None = None,
    query_string: bytes = b"",
    body: bytes = b"",
) -> Request:
    sent = False

    async def receive() -> dict[str, object]:
        nonlocal sent
        if sent:
            return {"type": "http.request", "body": b"", "more_body": False}
        sent = True
        return {"type": "http.request", "body": body, "more_body": False}

    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": f"/{path_prefix}",
        "raw_path": f"/{path_prefix}".encode(),
        "root_path": "",
        "query_string": query_string,
        "headers": [
            (name.casefold().encode("latin-1"), value.encode("latin-1"))
            for name, value in (headers or {}).items()
        ],
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "path_params": {"path_prefix": path_prefix},
    }
    return Request(scope, receive)


async def _open_bridge(
    router: MCPRouter,
    path_prefix: str,
) -> tuple[LegacySSEBridge, BridgeSession, AsyncGenerator[bytes]]:
    response = await router.catch_all_proxy(
        _request(
            "GET",
            path_prefix,
            headers={"accept": "text/event-stream"},
        )
    )
    assert isinstance(response, StreamingResponse)
    bridge = router._legacy_bridge
    assert bridge is not None
    session = next(
        session
        for session in bridge.sessions.values()
        if session.path_prefix == path_prefix
    )
    iterator = cast(AsyncGenerator[bytes], response.body_iterator)
    endpoint_event = await anext(iterator)
    assert f"/{path_prefix}?session_id={session.session_id}".encode() in endpoint_event
    return bridge, session, iterator


def _legacy_body(request_id: int = 1) -> bytes:
    return json.dumps(
        {
            "jsonrpc": "2.0",
            "id": request_id,
            "method": "tools/list",
            "params": {},
        },
        separators=(",", ":"),
    ).encode()


def test_importing_canonical_server_does_not_import_legacy_adapter() -> None:
    repo_root = Path(__file__).parents[1]
    result = subprocess.run(
        [
            sys.executable,
            "-c",
            (
                "import sys; import mcp_router.server; "
                "assert 'mcp_router.compat.legacy_sse' not in sys.modules"
            ),
        ],
        cwd=repo_root,
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 0, result.stderr


def test_legacy_bridge_requires_explicit_bounded_mapping() -> None:
    endpoint = _endpoint(
        bridge={
            "queue_capacity": 2,
            "backpressure_timeout": 0.05,
            "session_ttl": 1.0,
            "max_sessions": 3,
        }
    )
    config = endpoint.legacy_sse_bridge
    assert config is not None
    assert config.queue_capacity == 2
    assert config.backpressure_timeout == 0.05
    assert config.session_ttl == 1.0
    assert config.max_sessions == 3

    with pytest.raises(ValidationError):
        RouterConfig.model_validate(
            {
                "endpoints": [
                    {
                        "path": "legacy",
                        "mode": "remote",
                        "url": "https://legacy.example.test/mcp",
                        "summary": "Legacy fixture",
                        "legacy_sse_bridge": True,
                    }
                ]
            }
        )


@pytest.mark.asyncio
async def test_modern_streamable_http_never_instantiates_bridge_state() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = _endpoint(path="modern")
    router._configs = {"modern": endpoint}

    response = MagicMock()
    response.status_code = 200
    response.headers = httpx.Headers({"content-type": "application/json"})
    response.aread = AsyncMock(
        return_value=b'{"jsonrpc":"2.0","id":1,"result":{"resultType":"complete","tools":[]}}'
    )
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=response)
    stream_context.__aexit__ = AsyncMock(return_value=None)
    client = MagicMock()
    client.stream = MagicMock(return_value=stream_context)

    modern_body = json.dumps(
        {
            "jsonrpc": "2.0",
            "id": 1,
            "method": "tools/list",
            "params": {
                "_meta": {
                    "io.modelcontextprotocol/protocolVersion": "2026-07-28",
                    "io.modelcontextprotocol/clientCapabilities": {},
                }
            },
        },
        separators=(",", ":"),
    ).encode()
    request = _request(
        "POST",
        "modern",
        headers={
            "content-type": "application/json",
            "mcp-protocol-version": "2026-07-28",
            "mcp-method": "tools/list",
        },
        body=modern_body,
    )

    with patch.object(
        router,
        "_get_http_client",
        new_callable=AsyncMock,
        return_value=client,
    ):
        result = await router.catch_all_proxy(request)

    assert result.status_code == 200
    assert router._legacy_bridge is None


@pytest.mark.asyncio
async def test_bridge_queue_is_bounded_and_backpressure_fails_closed() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = _endpoint(
        bridge={
            "queue_capacity": 1,
            "backpressure_timeout": 0.01,
            "session_ttl": 60.0,
            "max_sessions": 2,
        }
    )
    router._configs = {"legacy": endpoint}
    bridge, session, iterator = await _open_bridge(router, "legacy")

    await session.queue.put(b"first")
    accepted = await bridge._enqueue_event(session, b"second")

    assert accepted is False
    assert session.closed.is_set()
    assert session.session_id not in bridge.sessions
    assert session.queue.qsize() == 0
    metrics = bridge.metrics_snapshot("legacy")
    assert metrics["backpressure_failures_total"] == 1

    terminal = await asyncio.wait_for(anext(iterator), timeout=0.2)
    assert b"event: error" in terminal
    assert b"backpressure_exceeded" in terminal
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)


@pytest.mark.asyncio
async def test_bridge_disconnect_cancels_response_and_closes_upstream() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = _endpoint(
        bridge={
            "queue_capacity": 4,
            "backpressure_timeout": 0.05,
            "session_ttl": 60.0,
            "max_sessions": 2,
        }
    )
    router._configs = {"legacy": endpoint}
    bridge, session, iterator = await _open_bridge(router, "legacy")

    response_started = asyncio.Event()
    never = asyncio.Event()

    async def upstream_lines():
        yield "event: message"
        yield 'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[]}}'
        yield ""
        response_started.set()
        await never.wait()

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.headers = httpx.Headers({"content-type": "text/event-stream"})
    upstream_response.aiter_lines = upstream_lines

    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=upstream_response)
    stream_context.__aexit__ = AsyncMock(return_value=None)
    client = MagicMock()
    client.stream = MagicMock(return_value=stream_context)

    with patch.object(
        router,
        "_get_http_client",
        new_callable=AsyncMock,
        return_value=client,
    ):
        bridge._client_provider = router._get_http_client
        post = await router.catch_all_proxy(
            _request(
                "POST",
                "legacy",
                headers={"content-type": "application/json"},
                query_string=f"session_id={session.session_id}".encode(),
                body=_legacy_body(),
            )
        )
        assert post.status_code == 202
        await asyncio.wait_for(response_started.wait(), timeout=0.2)
        assert session.runtime.active_leases == 1

        await iterator.aclose()

    await asyncio.sleep(0)
    assert session.session_id not in bridge.sessions
    assert session.runtime.legacy_session_ids == set()
    assert session.runtime.legacy_tasks == set()
    assert session.runtime.active_leases == 0
    assert session.queue.empty()
    stream_context.__aexit__.assert_awaited_once_with(None, None, None)
    assert bridge.metrics_snapshot("legacy")["downstream_disconnects_total"] == 1


@pytest.mark.asyncio
async def test_bridge_disconnect_during_upstream_setup_cannot_recreate_owned_work() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = _endpoint(path="legacy", bridge={"max_sessions": 2})
    router._configs = {"legacy": endpoint}
    bridge, session, iterator = await _open_bridge(router, "legacy")

    setup_started = asyncio.Event()
    setup_cancelled = asyncio.Event()
    release_setup = asyncio.Event()

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.headers = httpx.Headers({"content-type": "text/event-stream"})

    async def blocked_enter():
        setup_started.set()
        try:
            await release_setup.wait()
        except asyncio.CancelledError:
            setup_cancelled.set()
            await release_setup.wait()
        return upstream_response

    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(side_effect=blocked_enter)
    stream_context.__aexit__ = AsyncMock(return_value=None)
    client = MagicMock()
    client.stream = MagicMock(return_value=stream_context)
    bridge._client_provider = AsyncMock(return_value=client)

    post_task = asyncio.create_task(
        router.catch_all_proxy(
            _request(
                "POST",
                "legacy",
                headers={"content-type": "application/json"},
                query_string=f"session_id={session.session_id}".encode(),
                body=_legacy_body(),
            )
        )
    )
    await asyncio.wait_for(setup_started.wait(), timeout=0.2)
    assert session.runtime.active_leases == 1

    close_task = asyncio.create_task(iterator.aclose())
    await asyncio.wait_for(setup_cancelled.wait(), timeout=0.2)
    release_setup.set()

    await asyncio.wait_for(close_task, timeout=0.2)
    post_response = await asyncio.wait_for(post_task, timeout=0.2)

    assert post_response.status_code == 410
    assert session.closed.is_set()
    assert session.session_id not in bridge.sessions
    assert session.setup_tasks == set()
    assert session.response_tasks == set()
    assert session.runtime.legacy_tasks == set()
    assert session.runtime.active_leases == 0
    assert session.upstreams == set()
    stream_context.__aexit__.assert_awaited_once_with(None, None, None)


@pytest.mark.asyncio
async def test_runtime_retirement_during_bridge_setup_cannot_deadlock() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = _endpoint(path="legacy", bridge={"max_sessions": 2})
    router._configs = {"legacy": endpoint}
    bridge, session, iterator = await _open_bridge(router, "legacy")
    old_runtime = session.runtime

    setup_started = asyncio.Event()
    setup_cancelled = asyncio.Event()
    release_setup = asyncio.Event()

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.headers = httpx.Headers({"content-type": "text/event-stream"})

    async def blocked_enter():
        setup_started.set()
        try:
            await release_setup.wait()
        except asyncio.CancelledError:
            setup_cancelled.set()
            await release_setup.wait()
        return upstream_response

    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(side_effect=blocked_enter)
    stream_context.__aexit__ = AsyncMock(return_value=None)
    client = MagicMock()
    client.stream = MagicMock(return_value=stream_context)
    bridge._client_provider = AsyncMock(return_value=client)

    post_task = asyncio.create_task(
        router.catch_all_proxy(
            _request(
                "POST",
                "legacy",
                headers={"content-type": "application/json"},
                query_string=f"session_id={session.session_id}".encode(),
                body=_legacy_body(),
            )
        )
    )
    await asyncio.wait_for(setup_started.wait(), timeout=0.2)
    assert old_runtime.active_leases == 1

    replacement = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "legacy",
                    "mode": "remote",
                    "url": "https://replacement.example.test/mcp",
                    "summary": "Replacement fixture",
                    "transport": "streamable-http",
                    "legacy_sse_bridge": {},
                }
            ]
        }
    )
    reload_task = asyncio.create_task(router.apply_configuration(replacement))
    await asyncio.wait_for(setup_cancelled.wait(), timeout=0.2)
    release_setup.set()

    post_response = await asyncio.wait_for(post_task, timeout=0.2)
    await asyncio.wait_for(reload_task, timeout=0.2)

    assert post_response.status_code == 503
    assert old_runtime.active_leases == 0
    assert old_runtime.legacy_tasks == set()
    assert session.setup_tasks == set()
    assert session.response_tasks == set()
    assert session.session_id not in bridge.sessions
    assert session.upstreams == set()
    assert router._runtimes["legacy"] is not old_runtime
    stream_context.__aexit__.assert_awaited_once_with(None, None, None)
    await iterator.aclose()


@pytest.mark.asyncio
async def test_bridge_ttl_expires_session_and_rejects_replay() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = _endpoint(
        bridge={
            "queue_capacity": 2,
            "backpressure_timeout": 0.05,
            "session_ttl": 0.02,
            "max_sessions": 2,
        }
    )
    router._configs = {"legacy": endpoint}
    bridge, session, iterator = await _open_bridge(router, "legacy")

    terminal = await asyncio.wait_for(anext(iterator), timeout=0.3)
    assert b"event: error" in terminal
    assert b"session_expired" in terminal
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)

    replay = await router.catch_all_proxy(
        _request(
            "POST",
            "legacy",
            headers={"content-type": "application/json"},
            query_string=f"session_id={session.session_id}".encode(),
            body=_legacy_body(),
        )
    )
    assert replay.status_code == 410
    assert session.session_id not in bridge.sessions
    assert session.runtime.legacy_session_ids == set()
    assert bridge.metrics_snapshot("legacy")["expired_sessions_total"] == 1


@pytest.mark.asyncio
async def test_bridge_session_limit_rejects_excess_admission() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = _endpoint(
        bridge={
            "queue_capacity": 2,
            "backpressure_timeout": 0.05,
            "session_ttl": 60.0,
            "max_sessions": 1,
        }
    )
    router._configs = {"legacy": endpoint}
    bridge, session, iterator = await _open_bridge(router, "legacy")

    rejected = await router.catch_all_proxy(
        _request(
            "GET",
            "legacy",
            headers={"accept": "text/event-stream"},
        )
    )
    assert rejected.status_code == 429
    assert list(bridge.sessions) == [session.session_id]
    assert bridge.metrics_snapshot("legacy")["session_limit_rejections_total"] == 1

    await iterator.aclose()


@pytest.mark.asyncio
async def test_bridge_session_cannot_cross_endpoint_boundary() -> None:
    router = MCPRouter(Starlette(), "unused")
    alpha = _endpoint(path="alpha", bridge={"max_sessions": 2})
    beta = _endpoint(path="beta", bridge={"max_sessions": 2})
    router._configs = {"alpha": alpha, "beta": beta}
    bridge, session, iterator = await _open_bridge(router, "alpha")

    with patch.object(
        router,
        "_get_http_client",
        new_callable=AsyncMock,
    ) as get_client:
        bridge._client_provider = get_client
        response = await router.catch_all_proxy(
            _request(
                "POST",
                "beta",
                headers={"content-type": "application/json"},
                query_string=f"session_id={session.session_id}".encode(),
                body=_legacy_body(),
            )
        )

    assert response.status_code == 409
    get_client.assert_not_awaited()
    assert session.session_id in bridge.sessions
    await iterator.aclose()


@pytest.mark.asyncio
async def test_upstream_http_failure_is_synchronous_and_observable() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = _endpoint(path="legacy", bridge={"max_sessions": 2})
    router._configs = {"legacy": endpoint}
    bridge, session, iterator = await _open_bridge(router, "legacy")

    upstream_response = MagicMock()
    upstream_response.status_code = 503
    upstream_response.headers = httpx.Headers({"content-type": "text/plain"})
    upstream_response.aread = AsyncMock(return_value=b"upstream overloaded")
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=upstream_response)
    stream_context.__aexit__ = AsyncMock(return_value=None)
    client = MagicMock()
    client.stream = MagicMock(return_value=stream_context)

    with patch.object(
        router,
        "_get_http_client",
        new_callable=AsyncMock,
        return_value=client,
    ):
        bridge._client_provider = router._get_http_client
        response = await router.catch_all_proxy(
            _request(
                "POST",
                "legacy",
                headers={"content-type": "application/json"},
                query_string=f"session_id={session.session_id}".encode(),
                body=_legacy_body(),
            )
        )

    assert response.status_code == 502
    assert response.body
    payload = json.loads(bytes(response.body))
    assert payload["upstream_status"] == 503
    assert "upstream overloaded" in payload["detail"]
    assert session.runtime.active_leases == 0
    stream_context.__aexit__.assert_awaited_once_with(None, None, None)

    summary = await router.get_summary(MagicMock())
    summary_payload = json.loads(bytes(summary.body))
    bridge_summary = summary_payload["endpoints"][0]["legacy_sse_bridge"]
    assert bridge_summary["enabled"] is True
    assert bridge_summary["active_sessions"] == 1
    assert bridge_summary["sessions_opened_total"] == 1
    assert bridge_summary["posts_total"] == 1
    assert bridge_summary["upstream_failures_total"] == 1

    await iterator.aclose()


@pytest.mark.asyncio
async def test_async_upstream_failure_reaches_downstream_error_event() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = _endpoint(path="legacy", bridge={"max_sessions": 2})
    router._configs = {"legacy": endpoint}
    bridge, session, iterator = await _open_bridge(router, "legacy")

    upstream_response = MagicMock()
    upstream_response.status_code = 200
    upstream_response.headers = httpx.Headers({"content-type": "application/json"})
    upstream_response.aread = AsyncMock(side_effect=httpx.ReadError("read failed"))
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=upstream_response)
    stream_context.__aexit__ = AsyncMock(return_value=None)
    client = MagicMock()
    client.stream = MagicMock(return_value=stream_context)

    with patch.object(
        router,
        "_get_http_client",
        new_callable=AsyncMock,
        return_value=client,
    ):
        bridge._client_provider = router._get_http_client
        response = await router.catch_all_proxy(
            _request(
                "POST",
                "legacy",
                headers={"content-type": "application/json"},
                query_string=f"session_id={session.session_id}".encode(),
                body=_legacy_body(),
            )
        )
        assert response.status_code == 202
        terminal = await asyncio.wait_for(anext(iterator), timeout=0.2)

    assert b"event: error" in terminal
    assert b"upstream_response_failed" in terminal
    with pytest.raises(StopAsyncIteration):
        await anext(iterator)
    assert session.session_id not in bridge.sessions
    assert session.runtime.active_leases == 0
    assert bridge.metrics_snapshot("legacy")["upstream_failures_total"] == 1
    stream_context.__aexit__.assert_awaited_once_with(None, None, None)
