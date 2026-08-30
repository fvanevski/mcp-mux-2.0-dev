from __future__ import annotations

import asyncio
import signal
import socket
import sys
import time
from collections.abc import AsyncGenerator
from pathlib import Path
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from mcp_router.core.config_loader import ManagedEndpointConfig, RouterConfig
from mcp_router.core.process_manager import ProcessManager
from mcp_router.core.runtime import EndpointRuntime, RuntimeState
from mcp_router.server import BridgeSession, MCPRouter


def _find_unused_loopback_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def managed_config(
    *,
    path: str = "managed",
    url: str = "http://localhost:3033/mcp",
    headers: dict[str, str] | None = None,
    legacy_fallback: bool = False,
    restart: dict[str, object] | None = None,
    legacy_sse_bridge: bool = False,
    readiness_timeout: float = 0.05,
    readiness_interval: float = 0.01,
) -> ManagedEndpointConfig:
    data: dict[str, object] = {
        "path": path,
        "mode": "managed_cli",
        "argv": ["example-mcp", "--serve"],
        "url": url,
        "summary": "Managed fixture",
        "timeout": 30,
        "legacy_sse_bridge": legacy_sse_bridge,
        "readiness": {
            "timeout": readiness_timeout,
            "interval": readiness_interval,
            "legacy_initialize_fallback": legacy_fallback,
        },
    }
    if headers is not None:
        data["headers"] = headers
    if restart is not None:
        data["restart"] = restart
    endpoint = RouterConfig.model_validate({"endpoints": [data]}).endpoints[0]
    assert isinstance(endpoint, ManagedEndpointConfig)
    return endpoint


def _proxy_request(
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

    encoded_headers = [
        (name.casefold().encode("latin-1"), value.encode("latin-1"))
        for name, value in (headers or {}).items()
    ]
    scope = {
        "type": "http",
        "http_version": "1.1",
        "method": method,
        "scheme": "http",
        "path": f"/{path_prefix}",
        "raw_path": f"/{path_prefix}".encode(),
        "root_path": "",
        "query_string": query_string,
        "headers": encoded_headers,
        "server": ("testserver", 80),
        "client": ("127.0.0.1", 12345),
        "path_params": {"path_prefix": path_prefix},
    }
    return Request(scope, receive)


def test_managed_target_defaults_to_loopback_only() -> None:
    with pytest.raises(ValidationError, match="loopback"):
        managed_config(url="http://example.test:3033/mcp")

    config = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "managed",
                    "mode": "managed_cli",
                    "argv": ["example-mcp"],
                    "url": "http://example.test:3033/mcp",
                    "summary": "Explicit non-loopback fixture",
                    "allow_non_loopback_target": True,
                }
            ]
        }
    )
    endpoint = config.endpoints[0]
    assert isinstance(endpoint, ManagedEndpointConfig)
    assert endpoint.allow_non_loopback_target is True


def test_restart_backoff_is_bounded_and_validated() -> None:
    endpoint = managed_config(
        restart={
            "enabled": True,
            "max_attempts": 3,
            "initial_backoff": 0.25,
            "max_backoff": 0.5,
        }
    )
    manager = ProcessManager()

    assert manager._restart_delay(endpoint, 1) == 0.25
    assert manager._restart_delay(endpoint, 2) == 0.5
    assert manager._restart_delay(endpoint, 8) == 0.5

    with pytest.raises(ValidationError, match="max_backoff"):
        managed_config(
            restart={
                "enabled": True,
                "initial_backoff": 2.0,
                "max_backoff": 1.0,
            }
        )


@pytest.mark.asyncio
async def test_runtime_lease_spans_stream_lifetime() -> None:
    remote = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "remote",
                    "mode": "remote",
                    "url": "https://example.test/mcp",
                    "summary": "Remote fixture",
                }
            ]
        }
    ).endpoints[0]
    runtime = EndpointRuntime.from_config(remote)
    router = MCPRouter(Starlette(), "unused")

    lease = await runtime.acquire_lease()

    async def body():
        yield b"first"
        await asyncio.Event().wait()

    response: Response = await router._finish_leased_response(StreamingResponse(body()), lease)
    assert isinstance(response, StreamingResponse)
    iterator = cast(AsyncGenerator[bytes], response.body_iterator)

    assert await anext(iterator) == b"first"
    assert runtime.active_leases == 1

    await iterator.aclose()
    assert runtime.active_leases == 0


@pytest.mark.asyncio
async def test_reload_drains_active_runtime_before_atomic_publication() -> None:
    router = MCPRouter(Starlette(), "unused")
    old_config = managed_config(url="http://localhost:3033/mcp")
    new_config = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "managed",
                    "mode": "managed_cli",
                    "argv": ["example-mcp", "--serve"],
                    "url": "http://localhost:4040/mcp",
                    "summary": "Replacement fixture",
                }
            ]
        }
    )
    router._configs = {"managed": old_config}
    old_runtime = router._runtimes["managed"]
    old_runtime.state = RuntimeState.RUNNING
    lease = await old_runtime.acquire_lease()
    drain_started = asyncio.Event()

    async def drain(
        runtime: EndpointRuntime,
        *,
        final_state: RuntimeState = RuntimeState.STOPPED,
    ) -> None:
        runtime.state = RuntimeState.DRAINING
        drain_started.set()
        await runtime.wait_for_leases()
        runtime.state = final_state

    with patch.object(
        router.process_manager,
        "drain_and_stop",
        new_callable=AsyncMock,
        side_effect=drain,
    ) as drain_and_stop:
        reload_task = asyncio.create_task(router.apply_configuration(new_config))
        await drain_started.wait()

        assert router._runtimes["managed"] is old_runtime
        assert router._runtimes["managed"].config.url == "http://localhost:3033/mcp"
        assert not reload_task.done()

        await lease.release()
        await reload_task

    replacement = router._runtimes["managed"]
    assert replacement is not old_runtime
    assert replacement.config.url == "http://localhost:4040/mcp"
    drain_and_stop.assert_awaited_once_with(
        old_runtime,
        final_state=RuntimeState.DRAINING,
    )


@pytest.mark.asyncio
async def test_multi_endpoint_reload_keeps_retired_runtime_unavailable_until_publication() -> None:
    router = MCPRouter(Starlette(), "unused")
    old_alpha = managed_config(path="alpha", url="http://localhost:3033/mcp")
    old_beta = managed_config(path="beta", url="http://localhost:3034/mcp")
    router._configs = {"alpha": old_alpha, "beta": old_beta}
    alpha_runtime = router._runtimes["alpha"]
    beta_runtime = router._runtimes["beta"]
    alpha_runtime.state = RuntimeState.RUNNING
    beta_runtime.state = RuntimeState.RUNNING
    beta_lease = await beta_runtime.acquire_lease()
    alpha_drained = asyncio.Event()

    replacement = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "alpha",
                    "mode": "managed_cli",
                    "argv": ["example-mcp", "--serve"],
                    "url": "http://localhost:4040/mcp",
                    "summary": "Alpha replacement",
                },
                {
                    "path": "beta",
                    "mode": "managed_cli",
                    "argv": ["example-mcp", "--serve"],
                    "url": "http://localhost:4041/mcp",
                    "summary": "Beta replacement",
                },
            ]
        }
    )

    async def drain(
        runtime: EndpointRuntime,
        *,
        final_state: RuntimeState = RuntimeState.STOPPED,
    ) -> None:
        runtime.state = RuntimeState.DRAINING
        await runtime.wait_for_leases()
        runtime.state = final_state
        if runtime.path == "alpha":
            alpha_drained.set()

    with patch.object(
        router.process_manager,
        "drain_and_stop",
        new_callable=AsyncMock,
        side_effect=drain,
    ):
        reload_task = asyncio.create_task(router.apply_configuration(replacement))
        await alpha_drained.wait()

        assert router._runtimes["alpha"] is alpha_runtime
        assert alpha_runtime.state is RuntimeState.DRAINING
        with patch.object(
            router.process_manager,
            "start_managed_server",
            new_callable=AsyncMock,
        ) as start_managed:
            lease, rejection = await router._acquire_upstream_lease(alpha_runtime)

        assert lease is None
        assert rejection is not None
        assert rejection.status_code == 503
        start_managed.assert_not_awaited()
        assert not reload_task.done()

        await beta_lease.release()
        await reload_task

    assert router._runtimes["alpha"] is not alpha_runtime
    assert router._runtimes["alpha"].config.url == "http://localhost:4040/mcp"


@pytest.mark.asyncio
async def test_retired_runtime_rejects_legacy_session_after_cleanup_before_publication() -> None:
    router = MCPRouter(Starlette(), "unused")
    old_alpha = managed_config(
        path="alpha",
        url="http://localhost:3033/mcp",
        legacy_sse_bridge=True,
    )
    old_beta = managed_config(path="beta", url="http://localhost:3034/mcp")
    router._configs = {"alpha": old_alpha, "beta": old_beta}
    alpha_runtime = router._runtimes["alpha"]
    beta_runtime = router._runtimes["beta"]

    old_session_id = "alpha-before-retirement"
    router.active_sessions[old_session_id] = BridgeSession(
        path_prefix="alpha",
        queue=asyncio.Queue(),
    )
    alpha_runtime.legacy_session_ids.add(old_session_id)

    beta_drain_waiting = asyncio.Event()
    allow_beta_drain = asyncio.Event()

    async def blocked_beta_wait_for_leases() -> None:
        beta_drain_waiting.set()
        await allow_beta_drain.wait()

    replacement = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "alpha",
                    "mode": "managed_cli",
                    "argv": ["example-mcp", "--serve"],
                    "url": "http://localhost:4040/mcp",
                    "summary": "Alpha replacement",
                    "legacy_sse_bridge": True,
                },
                {
                    "path": "beta",
                    "mode": "managed_cli",
                    "argv": ["example-mcp", "--serve"],
                    "url": "http://localhost:4041/mcp",
                    "summary": "Beta replacement",
                },
            ]
        }
    )

    with patch.object(
        beta_runtime,
        "wait_for_leases",
        new_callable=AsyncMock,
        side_effect=blocked_beta_wait_for_leases,
    ) as beta_wait:
        reload_task = asyncio.create_task(router.apply_configuration(replacement))
        await beta_drain_waiting.wait()

        assert alpha_runtime.state is RuntimeState.DRAINING
        assert old_session_id not in router.active_sessions
        assert old_session_id not in alpha_runtime.legacy_session_ids
        assert router._runtimes["alpha"] is alpha_runtime
        assert not reload_task.done()

        response = await router.catch_all_proxy(
            _proxy_request(
                "GET",
                "alpha",
                headers={"accept": "text/event-stream"},
            )
        )

        assert response.status_code == 503
        assert router.active_sessions == {}
        assert alpha_runtime.legacy_session_ids == set()
        assert not reload_task.done()

        allow_beta_drain.set()
        await reload_task

    beta_wait.assert_awaited_once_with()
    assert router._runtimes["alpha"] is not alpha_runtime
    assert router._runtimes["alpha"].config.url == "http://localhost:4040/mcp"


@pytest.mark.asyncio
async def test_disabling_legacy_bridge_drops_session_and_rejects_stale_session_post() -> None:
    router = MCPRouter(Starlette(), "unused")
    old_config = managed_config(legacy_sse_bridge=True)
    router._configs = {"managed": old_config}
    old_runtime = router._runtimes["managed"]
    session_id = "legacy-before-disable"
    stale_session = BridgeSession(path_prefix="managed", queue=asyncio.Queue())
    router.active_sessions[session_id] = stale_session
    old_runtime.legacy_session_ids.add(session_id)

    replacement = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "managed",
                    "mode": "managed_cli",
                    "argv": ["example-mcp", "--serve"],
                    "url": "http://localhost:3033/mcp",
                    "summary": "Bridge disabled replacement",
                    "legacy_sse_bridge": False,
                }
            ]
        }
    )
    await router.apply_configuration(replacement)

    replacement_runtime = router._runtimes["managed"]
    assert replacement_runtime is not old_runtime
    assert replacement_runtime.config.legacy_sse_bridge is False
    assert session_id not in router.active_sessions
    assert session_id not in old_runtime.legacy_session_ids

    # Defense in depth: even if stale same-endpoint session state is reintroduced,
    # the replacement configuration remains authoritative and the bridge is unusable.
    router.active_sessions[session_id] = stale_session
    replacement_runtime.legacy_session_ids.add(session_id)
    request = _proxy_request(
        "POST",
        "managed",
        headers={"content-type": "application/json"},
        query_string=f"session_id={session_id}".encode(),
        body=b'{"jsonrpc":"2.0","id":1,"method":"tools/list"}',
    )

    with (
        patch.object(
            router.process_manager,
            "start_managed_server",
            new_callable=AsyncMock,
        ) as start_managed,
        patch.object(router, "_bridge_post", new_callable=AsyncMock) as bridge_post,
    ):
        response = await router.catch_all_proxy(request)

    assert response.status_code == 400
    start_managed.assert_not_awaited()
    bridge_post.assert_not_awaited()
    assert session_id not in router.active_sessions
    assert session_id not in replacement_runtime.legacy_session_ids


@pytest.mark.asyncio
async def test_stopped_retired_runtime_is_draining_before_first_await() -> None:
    router = MCPRouter(Starlette(), "unused")
    old_config = managed_config(url="http://localhost:3033/mcp")
    replacement = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "managed",
                    "mode": "managed_cli",
                    "argv": ["example-mcp", "--serve"],
                    "url": "http://localhost:4040/mcp",
                    "summary": "Replacement fixture",
                }
            ]
        }
    )
    router._configs = {"managed": old_config}
    old_runtime = router._runtimes["managed"]
    assert old_runtime.state is RuntimeState.STOPPED

    drain_waiting = asyncio.Event()
    allow_drain = asyncio.Event()

    async def blocked_wait_for_leases() -> None:
        drain_waiting.set()
        await allow_drain.wait()

    with (
        patch.object(
            old_runtime,
            "wait_for_leases",
            new_callable=AsyncMock,
            side_effect=blocked_wait_for_leases,
        ) as wait_for_leases,
        patch.object(
            router.process_manager,
            "start_managed_server",
            new_callable=AsyncMock,
        ) as start_managed,
    ):
        reload_task = asyncio.create_task(router.apply_configuration(replacement))
        await drain_waiting.wait()

        assert router._runtimes["managed"] is old_runtime
        assert old_runtime.state is RuntimeState.DRAINING
        assert not reload_task.done()

        lease, rejection = await router._acquire_upstream_lease(old_runtime)
        assert lease is None
        assert rejection is not None
        assert rejection.status_code == 503
        start_managed.assert_not_awaited()

        allow_drain.set()
        await reload_task

    wait_for_leases.assert_awaited_once_with()
    assert old_runtime.state is RuntimeState.DRAINING
    assert router._runtimes["managed"] is not old_runtime
    assert router._runtimes["managed"].config.url == "http://localhost:4040/mcp"


@pytest.mark.asyncio
async def test_local_legacy_sse_connection_does_not_start_or_lease_managed_runtime() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = managed_config(legacy_sse_bridge=True)
    router._configs = {"managed": endpoint}
    runtime = router._runtimes["managed"]

    request = MagicMock(spec=Request)
    request.method = "GET"
    request.path_params = {"path_prefix": "managed"}
    request.url.path = "/managed"
    request.headers = {"accept": "text/event-stream"}
    request.query_params = {}

    with patch.object(
        router.process_manager,
        "start_managed_server",
        new_callable=AsyncMock,
    ) as start_managed:
        response = await router.catch_all_proxy(request)

    assert isinstance(response, StreamingResponse)
    assert runtime.state is RuntimeState.STOPPED
    assert runtime.active_leases == 0
    start_managed.assert_not_awaited()
    body_iterator = cast(AsyncGenerator[bytes], response.body_iterator)
    await body_iterator.aclose()


@pytest.mark.asyncio
async def test_readiness_requires_mcp_after_tcp_opens() -> None:
    manager = ProcessManager()
    endpoint = managed_config()
    runtime = EndpointRuntime.from_config(endpoint)
    runtime.process = MagicMock(returncode=None)

    with (
        patch.object(manager, "_wait_for_port", new_callable=AsyncMock, return_value=True),
        patch.object(
            manager,
            "_probe_modern_discovery",
            new_callable=AsyncMock,
            return_value=False,
        ) as discover,
        patch.object(
            manager,
            "_probe_legacy_initialize",
            new_callable=AsyncMock,
            return_value=False,
        ) as legacy,
    ):
        assert await manager._wait_for_readiness(runtime, endpoint) is False

    assert discover.await_count >= 1
    legacy.assert_not_awaited()


@pytest.mark.asyncio
async def test_mcp_discovery_or_approved_legacy_fallback_establishes_readiness() -> None:
    manager = ProcessManager()
    endpoint = managed_config(legacy_fallback=True)
    runtime = EndpointRuntime.from_config(endpoint)
    runtime.process = MagicMock(returncode=None)

    with (
        patch.object(manager, "_wait_for_port", new_callable=AsyncMock, return_value=True),
        patch.object(
            manager,
            "_probe_modern_discovery",
            new_callable=AsyncMock,
            return_value=False,
        ) as discover,
        patch.object(
            manager,
            "_probe_legacy_initialize",
            new_callable=AsyncMock,
            return_value=True,
        ) as legacy,
    ):
        assert await manager._wait_for_readiness(runtime, endpoint) is True

    discover.assert_awaited_once()
    legacy.assert_awaited_once()


@pytest.mark.asyncio
async def test_readiness_probe_uses_remaining_budget_not_poll_interval() -> None:
    manager = ProcessManager()
    endpoint = managed_config(readiness_timeout=0.3, readiness_interval=0.1)
    runtime = EndpointRuntime.from_config(endpoint)
    runtime.process = MagicMock(returncode=None)
    observed_timeouts: list[float] = []

    async def discovery(
        candidate: ManagedEndpointConfig,
        timeout: float,
    ) -> bool:
        assert candidate is endpoint
        observed_timeouts.append(timeout)
        return timeout > 0.2

    with (
        patch.object(manager, "_wait_for_port", new_callable=AsyncMock, return_value=True),
        patch.object(
            manager,
            "_probe_modern_discovery",
            new_callable=AsyncMock,
            side_effect=discovery,
        ),
    ):
        assert await manager._wait_for_readiness(runtime, endpoint) is True

    assert len(observed_timeouts) == 1
    assert observed_timeouts[0] > endpoint.readiness.interval


@pytest.mark.asyncio
async def test_readiness_stream_accepts_first_sse_result_without_waiting_for_eof() -> None:
    manager = ProcessManager()
    endpoint = managed_config(readiness_timeout=1.0)
    stream_closed = asyncio.Event()
    never = asyncio.Event()

    async def readiness_lines():
        yield "event: message"
        yield (
            'data: {"jsonrpc":"2.0","id":"mcp-mux-readiness",'
            '"result":{"supportedVersions":["2026-07-28"]}}'
        )
        yield ""
        await never.wait()

    response = MagicMock()
    response.status_code = 200
    response.headers = httpx.Headers({"content-type": "text/event-stream"})
    response.aiter_lines = readiness_lines

    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=response)

    async def close_stream(*args: object) -> None:
        del args
        stream_closed.set()

    stream_context.__aexit__ = AsyncMock(side_effect=close_stream)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.stream = MagicMock(return_value=stream_context)

    with patch(
        "mcp_router.core.process_manager.httpx.AsyncClient",
        return_value=client,
    ):
        assert await manager._probe_modern_discovery(endpoint, 1.0) is True

    assert stream_closed.is_set()


@pytest.mark.asyncio
async def test_readiness_probe_preserves_configured_headers_and_overrides_protocol_fields() -> None:
    manager = ProcessManager()
    endpoint = managed_config(
        headers={
            "Authorization": "Bearer readiness-secret",
            "Accept": "text/plain",
            "Mcp-Method": "wrong-method",
        },
        readiness_timeout=1.0,
    )
    response = MagicMock()
    response.status_code = 200
    response.headers = httpx.Headers({"content-type": "application/json"})
    response.aread = AsyncMock(return_value=b"{}")
    response.json = MagicMock(
        return_value={
            "jsonrpc": "2.0",
            "id": "mcp-mux-readiness",
            "result": {"supportedVersions": ["2026-07-28"]},
        }
    )
    stream_context = MagicMock()
    stream_context.__aenter__ = AsyncMock(return_value=response)
    stream_context.__aexit__ = AsyncMock(return_value=None)
    client = MagicMock()
    client.__aenter__ = AsyncMock(return_value=client)
    client.__aexit__ = AsyncMock(return_value=None)
    client.stream = MagicMock(return_value=stream_context)

    with patch(
        "mcp_router.core.process_manager.httpx.AsyncClient",
        return_value=client,
    ):
        assert await manager._probe_modern_discovery(endpoint, 1.0) is True

    sent_headers = {
        name.casefold(): value
        for name, value in client.stream.call_args.kwargs["headers"].items()
    }
    assert sent_headers["authorization"] == "Bearer readiness-secret"
    assert sent_headers["accept"] == "application/json, text/event-stream"
    assert sent_headers["mcp-method"] == "server/discover"
    assert sent_headers["mcp-protocol-version"] == "2026-07-28"


@pytest.mark.asyncio
async def test_unexpected_exit_marks_runtime_failed_and_records_status() -> None:
    manager = ProcessManager()
    endpoint = managed_config()
    runtime = EndpointRuntime.from_config(endpoint)
    runtime.state = RuntimeState.RUNNING

    process = MagicMock()
    process.returncode = 17
    process.wait = AsyncMock(return_value=17)
    runtime.process = process

    await manager._monitor_exit(runtime, process)

    assert runtime.state is RuntimeState.FAILED
    assert runtime.last_exit_code == 17
    assert runtime.failure_reason == "managed process exited unexpectedly with code 17"
    assert runtime.restart_task is None


@pytest.mark.asyncio
async def test_failed_runtime_rejects_demand_activation_without_resetting_restart_budget() -> None:
    manager = ProcessManager()
    router = MCPRouter(Starlette(), "unused")
    endpoint = managed_config(
        restart={
            "enabled": True,
            "max_attempts": 3,
            "initial_backoff": 0.25,
            "max_backoff": 0.5,
        }
    )
    runtime = EndpointRuntime.from_config(endpoint)
    runtime.state = RuntimeState.FAILED
    runtime.restart_attempts = 1
    restart_blocker = asyncio.Event()

    async def pending_restart() -> None:
        await restart_blocker.wait()

    restart_task = asyncio.create_task(pending_restart())
    runtime.restart_task = restart_task
    try:
        with patch.object(
            router.process_manager,
            "start_managed_server",
            new_callable=AsyncMock,
        ) as start_managed:
            for _ in range(3):
                lease, rejection = await router._acquire_upstream_lease(runtime)
                assert lease is None
                assert rejection is not None
                assert rejection.status_code == 503

        start_managed.assert_not_awaited()
        assert runtime.restart_attempts == 1
        assert runtime.restart_task is restart_task
        assert not restart_task.done()
        with pytest.raises(RuntimeError, match="is failed"):
            await manager.start_managed_server(runtime)
        assert runtime.restart_task is restart_task
        assert not restart_task.done()
    finally:
        restart_task.cancel()
        await asyncio.gather(restart_task, return_exceptions=True)
        runtime.restart_task = None


@pytest.mark.asyncio
async def test_idle_timeout_skips_active_stream_lease_then_stops_after_release() -> None:
    router = MCPRouter(Starlette(), "unused")
    endpoint = managed_config()
    router._configs = {endpoint.path: endpoint}
    runtime = router._runtimes[endpoint.path]
    runtime.state = RuntimeState.RUNNING
    runtime.process = MagicMock(returncode=None)
    runtime.last_completed_activity = time.monotonic() - endpoint.timeout - 1.0
    lease = await runtime.acquire_lease()

    async def run_one_iteration(delay: float) -> None:
        del delay
        router._running = False

    router._running = True
    with (
        patch("mcp_router.server.asyncio.sleep", side_effect=run_one_iteration),
        patch.object(
            router.process_manager,
            "stop_managed_server",
            new_callable=AsyncMock,
        ) as stop_managed,
    ):
        await router.idle_timeout_checker()
    stop_managed.assert_not_awaited()

    await lease.release()
    runtime.last_completed_activity = time.monotonic() - endpoint.timeout - 1.0
    router._running = True
    with (
        patch("mcp_router.server.asyncio.sleep", side_effect=run_one_iteration),
        patch.object(
            router.process_manager,
            "stop_managed_server",
            new_callable=AsyncMock,
        ) as stop_managed,
    ):
        await router.idle_timeout_checker()
    stop_managed.assert_awaited_once_with(runtime)


@pytest.mark.asyncio
async def test_concurrent_first_requests_spawn_only_one_process() -> None:
    manager = ProcessManager()
    endpoint = managed_config()
    runtime = EndpointRuntime.from_config(endpoint)
    process_exit = asyncio.Event()
    log_exit = asyncio.Event()

    async def wait_for_exit() -> int:
        await process_exit.wait()
        process.returncode = 0
        return 0

    async def read_log() -> bytes:
        await log_exit.wait()
        return b""

    process = MagicMock()
    process.pid = 43210
    process.returncode = None
    process.wait = AsyncMock(side_effect=wait_for_exit)
    process.stdout = MagicMock()
    process.stdout.readline = AsyncMock(side_effect=read_log)
    process.stderr = MagicMock()
    process.stderr.readline = AsyncMock(side_effect=read_log)

    with (
        patch("asyncio.create_subprocess_exec", return_value=process) as create_exec,
        patch.object(manager, "_wait_for_readiness", new_callable=AsyncMock, return_value=True),
    ):
        first, second = await asyncio.gather(
            manager.start_managed_server(runtime),
            manager.start_managed_server(runtime),
        )

    assert first == endpoint.url
    assert second == endpoint.url
    create_exec.assert_called_once()

    with patch("os.killpg", side_effect=lambda *_: process_exit.set()):
        await manager.stop_managed_server(runtime)


@pytest.mark.asyncio
async def test_cleanup_leaves_no_real_managed_subprocess_alive() -> None:
    manager = ProcessManager()
    port = _find_unused_loopback_port()
    fixture = Path(__file__).parent / "fixtures" / "managed_mcp_server.py"
    endpoint = ManagedEndpointConfig(
        path="managed-integration",
        mode="managed_cli",
        argv=[sys.executable, str(fixture), str(port)],
        url=f"http://127.0.0.1:{port}/mcp",
        summary="Disposable managed MCP integration fixture",
        readiness={"timeout": 5.0, "interval": 0.05},
    )
    runtime = EndpointRuntime.from_config(endpoint)
    process = None

    try:
        await manager.start_managed_server(runtime)
        process = runtime.process
        assert process is not None
        assert process.returncode is None
        assert runtime.state is RuntimeState.RUNNING
    finally:
        await manager.cleanup([runtime])

    assert process is not None
    assert process.returncode is not None
    assert runtime.process is None
    assert runtime.state is RuntimeState.STOPPED


@pytest.mark.asyncio
async def test_cleanup_terminates_process_and_awaits_owned_tasks() -> None:
    manager = ProcessManager()
    endpoint = managed_config()
    runtime = EndpointRuntime.from_config(endpoint)
    runtime.state = RuntimeState.RUNNING
    process_exit = asyncio.Event()
    task_exit = asyncio.Event()

    async def wait_for_exit() -> int:
        await process_exit.wait()
        process.returncode = 0
        return 0

    async def pending_task() -> None:
        await task_exit.wait()

    process = MagicMock()
    process.pid = 54321
    process.returncode = None
    process.wait = AsyncMock(side_effect=wait_for_exit)
    runtime.process = process
    runtime.stdout_task = asyncio.create_task(pending_task())
    runtime.stderr_task = asyncio.create_task(pending_task())
    runtime.exit_task = asyncio.create_task(manager._monitor_exit(runtime, process))

    with patch("os.killpg", side_effect=lambda *_: process_exit.set()) as killpg:
        await manager.cleanup([runtime])

    killpg.assert_called_once_with(54321, signal.SIGTERM)
    assert runtime.state is RuntimeState.STOPPED
    assert runtime.process is None
    assert runtime.stdout_task is None
    assert runtime.stderr_task is None
    assert runtime.exit_task is None
