from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Callable, Iterable
from typing import Any

import httpx

from .config_loader import ManagedEndpointConfig
from .protocol import (
    CLIENT_CAPABILITIES_META,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META,
)
from .runtime import EndpointRuntime, RuntimeState

logger = logging.getLogger(__name__)

_SHUTDOWN_TIMEOUT_SECONDS = 3.0
_READINESS_REQUEST_ID = "mcp-mux-readiness"
_LEGACY_READINESS_PROTOCOL_VERSION = "2025-11-25"


class ProcessManager:
    """Managed-process supervisor operating on endpoint-owned runtime state."""

    def __init__(self) -> None:
        self._redact: Callable[[str], str] = lambda text: text

    def set_redactor(self, redactor: Callable[[str], str]) -> None:
        self._redact = redactor

    @staticmethod
    def is_running(runtime: EndpointRuntime) -> bool:
        proc = runtime.process
        return proc is not None and proc.returncode is None

    async def start_managed_server(self, runtime: EndpointRuntime) -> str:
        async with runtime.lock:
            if runtime.state is RuntimeState.FAILED:
                raise RuntimeError(
                    f"Managed endpoint '{runtime.path}' is failed; "
                    "automatic recovery or runtime reconfiguration is required"
                )
            return await self._start_locked(runtime, reset_restart_attempts=True)

    async def _start_locked(
        self,
        runtime: EndpointRuntime,
        *,
        reset_restart_attempts: bool,
    ) -> str:
        endpoint_cfg = runtime.config
        if not isinstance(endpoint_cfg, ManagedEndpointConfig):
            raise TypeError(f"Endpoint '{runtime.path}' is not a managed endpoint")
        if self.is_running(runtime) and runtime.state is RuntimeState.RUNNING:
            return endpoint_cfg.url
        if runtime.state is RuntimeState.DRAINING:
            raise RuntimeError(f"Managed endpoint '{runtime.path}' is draining")

        current_task = asyncio.current_task()
        restart_task = runtime.restart_task
        if restart_task is not None and restart_task is not current_task and not restart_task.done():
            restart_task.cancel()
            await asyncio.gather(restart_task, return_exceptions=True)
            runtime.restart_task = None

        if reset_restart_attempts:
            runtime.restart_attempts = 0
        runtime.state = RuntimeState.STARTING
        runtime.failure_reason = None

        readiness = endpoint_cfg.readiness
        if readiness.host is None or readiness.port is None:
            runtime.state = RuntimeState.FAILED
            runtime.failure_reason = "unresolved readiness settings"
            raise RuntimeError(
                f"Managed endpoint '{runtime.path}' has unresolved readiness settings"
            )

        subprocess_env = os.environ.copy()
        subprocess_env.update(endpoint_cfg.env)
        start_new_session = os.name == "posix"

        try:
            if endpoint_cfg.argv is not None:
                logger.info(
                    "Spawning managed process for path '%s' using executable %r on %s:%s",
                    runtime.path,
                    endpoint_cfg.argv[0],
                    readiness.host,
                    readiness.port,
                )
                proc = await asyncio.create_subprocess_exec(
                    *endpoint_cfg.argv,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=endpoint_cfg.cwd,
                    env=subprocess_env,
                    start_new_session=start_new_session,
                )
            else:
                shell_command = endpoint_cfg.unsafe_shell_command
                if shell_command is None:
                    raise RuntimeError(
                        f"Managed endpoint '{runtime.path}' has no execution command"
                    )
                logger.warning(
                    "Spawning explicitly unsafe shell process for path '%s' on %s:%s",
                    runtime.path,
                    readiness.host,
                    readiness.port,
                )
                proc = await asyncio.create_subprocess_shell(
                    shell_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=endpoint_cfg.cwd,
                    env=subprocess_env,
                    start_new_session=start_new_session,
                )

            runtime.process = proc
            if proc.stdout is None or proc.stderr is None:
                raise RuntimeError(
                    f"Managed endpoint '{runtime.path}' did not expose piped output streams"
                )
            runtime.stdout_task = asyncio.create_task(
                self._stream_logs(proc.stdout, f"{runtime.path}:stdout"),
                name=f"mcp-mux:{runtime.path}:stdout",
            )
            runtime.stderr_task = asyncio.create_task(
                self._stream_logs(proc.stderr, f"{runtime.path}:stderr"),
                name=f"mcp-mux:{runtime.path}:stderr",
            )
            runtime.exit_task = asyncio.create_task(
                self._monitor_exit(runtime, proc),
                name=f"mcp-mux:{runtime.path}:exit",
            )

            if not await self._wait_for_readiness(runtime, endpoint_cfg):
                if proc.returncode is not None:
                    raise RuntimeError(
                        f"Process terminated before readiness with exit code {proc.returncode}"
                    )
                raise TimeoutError(
                    f"MCP service at {endpoint_cfg.url} failed readiness within "
                    f"{readiness.timeout}s"
                )

            runtime.state = RuntimeState.RUNNING
            runtime.failure_reason = None
            runtime.last_completed_activity = asyncio.get_running_loop().time()
            logger.info(
                "Managed endpoint '%s' is MCP-ready at %s",
                runtime.path,
                endpoint_cfg.url,
            )
            return endpoint_cfg.url
        except asyncio.CancelledError:
            # Startup owns the spawned process and its tasks until readiness
            # succeeds. Request/supervisor cancellation must synchronously
            # relinquish that ownership before the runtime becomes retryable.
            runtime.failure_reason = None
            await self._terminate_locked(runtime, final_state=RuntimeState.STOPPED)
            raise
        except (OSError, RuntimeError, TimeoutError, ValueError, httpx.HTTPError) as exc:
            runtime.failure_reason = self._redact(str(exc))
            logger.error(
                "Failed to launch managed endpoint '%s': %s",
                runtime.path,
                runtime.failure_reason,
            )
            await self._terminate_locked(runtime, final_state=RuntimeState.FAILED)
            raise

    async def drain_and_stop(
        self,
        runtime: EndpointRuntime,
        *,
        final_state: RuntimeState = RuntimeState.STOPPED,
    ) -> None:
        async with runtime.lock:
            # Retirement must become non-activatable before the first await,
            # even when no managed process is currently running.
            runtime.state = RuntimeState.DRAINING
        await runtime.wait_for_leases()
        await runtime.cancel_legacy_tasks()
        await self.stop_managed_server(runtime, final_state=final_state)

    async def stop_managed_server(
        self,
        runtime: EndpointRuntime,
        *,
        final_state: RuntimeState = RuntimeState.STOPPED,
    ) -> None:
        async with runtime.lock:
            await self._terminate_locked(runtime, final_state=final_state)

    async def _terminate_locked(
        self,
        runtime: EndpointRuntime,
        *,
        final_state: RuntimeState,
    ) -> None:
        current_task = asyncio.current_task()

        restart_task = runtime.restart_task
        if restart_task is not None and restart_task is not current_task and not restart_task.done():
            restart_task.cancel()
            await asyncio.gather(restart_task, return_exceptions=True)
        if restart_task is not current_task:
            runtime.restart_task = None

        exit_task = runtime.exit_task
        if exit_task is not None and exit_task is not current_task and not exit_task.done():
            exit_task.cancel()
            await asyncio.gather(exit_task, return_exceptions=True)
        if exit_task is not current_task:
            runtime.exit_task = None

        proc = runtime.process
        if proc is not None and proc.returncode is None:
            logger.info(
                "Terminating managed process for '%s' (PID %s)",
                runtime.path,
                proc.pid,
            )
            try:
                if os.name == "posix" and hasattr(os, "killpg"):
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    proc.terminate()

                try:
                    await asyncio.wait_for(proc.wait(), timeout=_SHUTDOWN_TIMEOUT_SECONDS)
                except TimeoutError:
                    logger.warning(
                        "Process for '%s' (PID %s) did not exit; forcing termination",
                        runtime.path,
                        proc.pid,
                    )
                    if os.name == "posix" and hasattr(os, "killpg"):
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        proc.kill()
                    await proc.wait()
            except (OSError, RuntimeError) as exc:
                logger.error(
                    "Error terminating process for '%s': %s",
                    runtime.path,
                    self._redact(str(exc)),
                )

        await self._finish_log_tasks_locked(runtime)
        runtime.process = None
        runtime.state = final_state

    async def _finish_log_tasks_locked(self, runtime: EndpointRuntime) -> None:
        tasks = [
            task
            for task in (runtime.stdout_task, runtime.stderr_task)
            if task is not None
        ]
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        runtime.stdout_task = None
        runtime.stderr_task = None

    async def _monitor_exit(
        self,
        runtime: EndpointRuntime,
        proc: asyncio.subprocess.Process,
    ) -> None:
        returncode = await proc.wait()

        async with runtime.lock:
            if runtime.process is not proc:
                return
            runtime.last_exit_code = returncode
            runtime.process = None
            runtime.exit_task = None
            await self._finish_log_tasks_locked(runtime)

            if runtime.state in {RuntimeState.DRAINING, RuntimeState.STOPPED}:
                return

            runtime.state = RuntimeState.FAILED
            runtime.failure_reason = f"managed process exited unexpectedly with code {returncode}"
            logger.error(
                "Managed endpoint '%s' failed: %s",
                runtime.path,
                runtime.failure_reason,
            )
            self._schedule_restart_locked(runtime)

    def _schedule_restart_locked(self, runtime: EndpointRuntime) -> None:
        endpoint_cfg = runtime.config
        if not isinstance(endpoint_cfg, ManagedEndpointConfig):
            return
        restart = endpoint_cfg.restart
        if not restart.enabled or runtime.restart_attempts >= restart.max_attempts:
            return
        existing = runtime.restart_task
        if existing is not None and not existing.done():
            return
        runtime.restart_task = asyncio.create_task(
            self._restart_loop(runtime),
            name=f"mcp-mux:{runtime.path}:restart",
        )

    async def _restart_loop(self, runtime: EndpointRuntime) -> None:
        current_task = asyncio.current_task()
        try:
            while True:
                async with runtime.lock:
                    endpoint_cfg = runtime.config
                    if not isinstance(endpoint_cfg, ManagedEndpointConfig):
                        return
                    restart = endpoint_cfg.restart
                    if (
                        runtime.state is not RuntimeState.FAILED
                        or not restart.enabled
                        or runtime.restart_attempts >= restart.max_attempts
                    ):
                        return
                    runtime.restart_attempts += 1
                    attempt = runtime.restart_attempts
                    delay = self._restart_delay(endpoint_cfg, attempt)

                logger.warning(
                    "Restarting managed endpoint '%s' after %.3fs (attempt %s/%s)",
                    runtime.path,
                    delay,
                    attempt,
                    restart.max_attempts,
                )
                await asyncio.sleep(delay)

                async with runtime.lock:
                    endpoint_cfg = runtime.config
                    if not isinstance(endpoint_cfg, ManagedEndpointConfig):
                        return
                    restart = endpoint_cfg.restart
                    if (
                        runtime.state is not RuntimeState.FAILED
                        or not restart.enabled
                        or attempt > restart.max_attempts
                    ):
                        return
                    try:
                        await self._start_locked(
                            runtime,
                            reset_restart_attempts=False,
                        )
                    except (OSError, RuntimeError, TimeoutError, ValueError, httpx.HTTPError):
                        continue

                    if runtime.restart_task is current_task:
                        runtime.restart_task = None
                    return
        finally:
            if runtime.restart_task is current_task:
                runtime.restart_task = None

    @staticmethod
    def _restart_delay(endpoint_cfg: ManagedEndpointConfig, attempt: int) -> float:
        restart = endpoint_cfg.restart
        exponent = max(0, attempt - 1)
        return min(restart.initial_backoff * (2**exponent), restart.max_backoff)

    async def _wait_for_readiness(
        self,
        runtime: EndpointRuntime,
        endpoint_cfg: ManagedEndpointConfig,
    ) -> bool:
        readiness = endpoint_cfg.readiness
        if readiness.host is None or readiness.port is None:
            return False

        loop = asyncio.get_running_loop()
        deadline = loop.time() + readiness.timeout
        remaining = deadline - loop.time()
        if remaining <= 0:
            return False
        if not await self._wait_for_port(
            readiness.port,
            host=readiness.host,
            timeout=remaining,
            interval=readiness.interval,
        ):
            return False

        while True:
            proc = runtime.process
            if proc is None or proc.returncode is not None:
                return False
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            if await self._probe_modern_discovery(endpoint_cfg, remaining):
                return True
            if readiness.legacy_initialize_fallback:
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                if await self._probe_legacy_initialize(endpoint_cfg, remaining):
                    return True
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            await asyncio.sleep(min(readiness.interval, remaining))

    @staticmethod
    def _merge_probe_headers(
        endpoint_cfg: ManagedEndpointConfig,
        required: dict[str, str],
    ) -> dict[str, str]:
        required_names = {name.casefold() for name in required}
        headers = {
            name: value
            for name, value in (endpoint_cfg.headers or {}).items()
            if name.casefold() not in required_names
        }
        headers.update(required)
        return headers

    async def _probe_modern_discovery(
        self,
        endpoint_cfg: ManagedEndpointConfig,
        timeout: float,
    ) -> bool:
        payload = {
            "jsonrpc": "2.0",
            "id": _READINESS_REQUEST_ID,
            "method": "server/discover",
            "params": {
                "_meta": {
                    PROTOCOL_VERSION_META: MODERN_PROTOCOL_VERSION,
                    CLIENT_CAPABILITIES_META: {},
                }
            },
        }
        headers = self._merge_probe_headers(
            endpoint_cfg,
            {
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
                "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                "Mcp-Method": "server/discover",
            },
        )
        return await self._probe_jsonrpc_result(endpoint_cfg.url, payload, headers, timeout)

    async def _probe_legacy_initialize(
        self,
        endpoint_cfg: ManagedEndpointConfig,
        timeout: float,
    ) -> bool:
        payload = {
            "jsonrpc": "2.0",
            "id": _READINESS_REQUEST_ID,
            "method": "initialize",
            "params": {
                "protocolVersion": _LEGACY_READINESS_PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcp-mux-readiness", "version": "0"},
            },
        }
        headers = self._merge_probe_headers(
            endpoint_cfg,
            {
                "content-type": "application/json",
                "accept": "application/json, text/event-stream",
            },
        )
        return await self._probe_jsonrpc_result(endpoint_cfg.url, payload, headers, timeout)

    async def _probe_jsonrpc_result(
        self,
        url: str,
        payload: dict[str, Any],
        headers: dict[str, str],
        timeout: float,
    ) -> bool:
        try:
            async with asyncio.timeout(timeout):
                async with httpx.AsyncClient(
                    timeout=httpx.Timeout(timeout),
                    follow_redirects=False,
                ) as client:
                    async with client.stream(
                        "POST",
                        url,
                        json=payload,
                        headers=headers,
                    ) as response:
                        if not 200 <= response.status_code < 300:
                            return False
                        response_payload = await self._extract_jsonrpc_payload(response)
        except (TimeoutError, httpx.HTTPError, OSError, RuntimeError, ValueError):
            return False
        return (
            isinstance(response_payload, dict)
            and response_payload.get("jsonrpc") == "2.0"
            and response_payload.get("id") == _READINESS_REQUEST_ID
            and isinstance(response_payload.get("result"), dict)
        )

    @staticmethod
    async def _extract_jsonrpc_payload(response: httpx.Response) -> object | None:
        content_type = response.headers.get("content-type", "").casefold()
        if "event-stream" not in content_type:
            try:
                await response.aread()
                return response.json()
            except ValueError:
                return None

        import json

        data_lines: list[str] = []
        async for line in response.aiter_lines():
            if not line:
                if not data_lines:
                    continue
                try:
                    return json.loads("\n".join(data_lines))
                except ValueError:
                    data_lines.clear()
                    continue
            if line.startswith("data:"):
                data_lines.append(line[5:].lstrip())
        if not data_lines:
            return None
        try:
            return json.loads("\n".join(data_lines))
        except ValueError:
            return None

    async def _stream_logs(self, stream: asyncio.StreamReader, prefix: str) -> None:
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                logger.info("[%s] %s", prefix, self._redact(decoded))
        except asyncio.CancelledError:
            raise
        except (OSError, RuntimeError) as exc:
            logger.error(
                "Error streaming logs for %s: %s",
                prefix,
                self._redact(str(exc)),
            )

    async def _wait_for_port(
        self,
        port: int,
        host: str = "127.0.0.1",
        timeout: float = 15.0,
        interval: float = 0.2,
    ) -> bool:
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                return False
            try:
                _, writer = await asyncio.wait_for(
                    asyncio.open_connection(host, port),
                    timeout=remaining,
                )
            except TimeoutError:
                return False
            except (ConnectionRefusedError, OSError):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(interval, remaining))
                continue

            writer.close()
            remaining = deadline - loop.time()
            if remaining > 0:
                try:
                    await asyncio.wait_for(writer.wait_closed(), timeout=remaining)
                except (OSError, TimeoutError):
                    pass
            return True

    async def cleanup(self, runtimes: Iterable[EndpointRuntime]) -> None:
        logger.info("Initiating cleanup of managed endpoint subprocesses.")
        for runtime in list(runtimes):
            if runtime.managed:
                await self.drain_and_stop(runtime)
        logger.info("Process manager cleanup complete.")
