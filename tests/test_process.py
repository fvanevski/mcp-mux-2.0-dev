from __future__ import annotations

import asyncio
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from mcp_router.core.config_loader import ManagedEndpointConfig
from mcp_router.core.process_manager import ProcessManager
from mcp_router.core.runtime import EndpointRuntime, RuntimeState


@pytest.mark.asyncio
async def test_wait_for_port_caps_sleep_at_readiness_deadline(monkeypatch):
    pm = ProcessManager()
    now = 0.0
    sleep_calls: list[float] = []

    class FakeLoop:
        def time(self) -> float:
            return now

    async def fail_connection(host: str, port: int):
        del host, port
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
        del host, port
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
    cfg = ManagedEndpointConfig(
        path="mock-mcp",
        mode="managed_cli",
        argv=["python", "-m", "http.server", "8099"],
        url="http://localhost:8099/mcp",
        summary="Mock python server",
    )
    runtime = EndpointRuntime.from_config(cfg)
    process_exit = asyncio.Event()
    log_exit = asyncio.Event()

    async def wait_for_exit() -> int:
        await process_exit.wait()
        mock_proc.returncode = 0
        return 0

    async def wait_for_log_eof() -> bytes:
        await log_exit.wait()
        return b""

    mock_proc = MagicMock()
    mock_proc.pid = 99999
    mock_proc.returncode = None
    mock_proc.wait = AsyncMock(side_effect=wait_for_exit)
    mock_proc.stdout = MagicMock()
    mock_proc.stdout.readline = AsyncMock(side_effect=wait_for_log_eof)
    mock_proc.stderr = MagicMock()
    mock_proc.stderr.readline = AsyncMock(side_effect=wait_for_log_eof)

    with (
        patch("asyncio.create_subprocess_exec", return_value=mock_proc) as mock_exec,
        patch.object(pm, "_wait_for_readiness", return_value=True) as mock_readiness,
    ):
        target_url = await pm.start_managed_server(runtime)
        assert target_url == "http://localhost:8099/mcp"
        assert mock_exec.call_args.args == ("python", "-m", "http.server", "8099")
        assert mock_exec.call_args.kwargs["start_new_session"] is True
        mock_readiness.assert_awaited_once_with(runtime, cfg)
        assert runtime.state is RuntimeState.RUNNING
        assert runtime.process is mock_proc

        with patch("os.killpg", side_effect=lambda *_: process_exit.set()) as mock_killpg:
            await pm.stop_managed_server(runtime)

        mock_killpg.assert_called_once_with(99999, 15)
        assert runtime.state is RuntimeState.STOPPED
        assert runtime.process is None
        assert runtime.stdout_task is None
        assert runtime.stderr_task is None
        assert runtime.exit_task is None
