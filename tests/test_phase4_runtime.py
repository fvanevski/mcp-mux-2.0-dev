from __future__ import annotations

import asyncio
import signal
from collections.abc import AsyncGenerator
from typing import cast
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pydantic import ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import Response, StreamingResponse

from mcp_router.core.config_loader import ManagedEndpointConfig, RouterConfig
from mcp_router.core.process_manager import ProcessManager
from mcp_router.core.runtime import EndpointRuntime, RuntimeState
from mcp_router.server import MCPRouter


def managed_config(
    *,
    url: str = "http://localhost:3033/mcp",
    legacy_fallback: bool = False,
    restart: dict[str, object] | None = None,
    legacy_sse_bridge: bool = False,
) -> ManagedEndpointConfig:
    data: dict[str, object] = {
        "path": "managed",
        "mode": "managed_cli",
        "argv": ["example-mcp", "--serve"],
        "url": url,
        "summary": "Managed fixture",
        "timeout": 30,
        "legacy_sse_bridge": legacy_sse_bridge,
        "readiness": {
            "timeout": 0.05,
            "interval": 0.01,
            "legacy_initialize_fallback": legacy_fallback,
        },
    }
    if restart is not None:
        data["restart"] = restart
    endpoint = RouterConfig.model_validate({"endpoints": [data]}).endpoints[0]
    assert isinstance(endpoint, ManagedEndpointConfig)
    return endpoint


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
    iterator = cast(AsyncGenerator[bytes, None], response.body_iterator)

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

    async def drain(runtime: EndpointRuntime) -> None:
        runtime.state = RuntimeState.DRAINING
        drain_started.set()
        await runtime.wait_for_leases()
        runtime.state = RuntimeState.STOPPED

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
    drain_and_stop.assert_awaited_once_with(old_runtime)


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
    body_iterator = cast(AsyncGenerator[bytes, None], response.body_iterator)
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
