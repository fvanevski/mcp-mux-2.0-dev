from __future__ import annotations

import asyncio
import logging
import os
import signal

from .config_loader import ManagedEndpointConfig

logger = logging.getLogger(__name__)


class ProcessManager:
    """Registry for managed local subprocess lifecycles."""

    _instance: ProcessManager | None = None
    _processes: dict[str, asyncio.subprocess.Process]
    _log_tasks: list[asyncio.Task[None]]

    def __new__(cls, *args, **kwargs):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._processes = {}
            cls._instance._log_tasks = []
        return cls._instance

    def __init__(self):
        if hasattr(self, "_initialized"):
            return
        self._initialized = True

    def is_running(self, path: str) -> bool:
        proc = self._processes.get(path)
        if proc is None:
            return False
        return proc.returncode is None

    async def start_managed_server(self, endpoint_cfg: ManagedEndpointConfig) -> str:
        path = endpoint_cfg.path
        if self.is_running(path):
            logger.info("Process for path '%s' is already running.", path)
            return endpoint_cfg.url

        readiness = endpoint_cfg.readiness
        if readiness.host is None or readiness.port is None:
            raise RuntimeError(f"Managed endpoint '{path}' has unresolved readiness settings")

        subprocess_env = os.environ.copy()
        subprocess_env.update(endpoint_cfg.env)
        preexec_fn = os.setsid if hasattr(os, "setsid") else None

        try:
            if endpoint_cfg.argv is not None:
                logger.info(
                    "Spawning managed process for path '%s' using executable %r on %s:%s",
                    path,
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
                    preexec_fn=preexec_fn,
                )
            else:
                shell_command = endpoint_cfg.unsafe_shell_command
                if shell_command is None:
                    raise RuntimeError(f"Managed endpoint '{path}' has no execution command")
                logger.warning(
                    "Spawning explicitly unsafe shell process for path '%s' on %s:%s",
                    path,
                    readiness.host,
                    readiness.port,
                )
                proc = await asyncio.create_subprocess_shell(
                    shell_command,
                    stdout=asyncio.subprocess.PIPE,
                    stderr=asyncio.subprocess.PIPE,
                    cwd=endpoint_cfg.cwd,
                    env=subprocess_env,
                    preexec_fn=preexec_fn,
                )

            self._processes[path] = proc
            if proc.stdout is None or proc.stderr is None:
                raise RuntimeError(f"Managed endpoint '{path}' did not expose piped output streams")
            task_stdout = asyncio.create_task(self._stream_logs(proc.stdout, f"{path}:stdout"))
            task_stderr = asyncio.create_task(self._stream_logs(proc.stderr, f"{path}:stderr"))
            self._log_tasks.extend([task_stdout, task_stderr])

            success = await self._wait_for_port(
                readiness.port,
                host=readiness.host,
                timeout=readiness.timeout,
                interval=readiness.interval,
            )
            if not success:
                if proc.returncode is not None:
                    raise RuntimeError(f"Process terminated instantly with exit code {proc.returncode}")
                raise TimeoutError(
                    f"Local HTTP service on {readiness.host}:{readiness.port} failed to become ready in time"
                )

            logger.info("Subserver for path '%s' is ready at %s", path, endpoint_cfg.url)
            return endpoint_cfg.url
        except (OSError, RuntimeError, TimeoutError, ValueError) as exc:
            logger.error("Failed to launch subserver for path '%s': %s", path, exc)
            await self.stop_managed_server(path)
            raise

    async def stop_managed_server(self, path: str):
        proc = self._processes.pop(path, None)
        if proc:
            logger.info("Terminating subprocess group for '%s' (PID %s)", path, proc.pid)
            try:
                if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                    try:
                        os.killpg(os.getpgid(proc.pid), signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:
                    proc.terminate()

                try:
                    await asyncio.wait_for(proc.wait(), timeout=3.0)
                except TimeoutError:
                    logger.warning("Process for '%s' (PID %s) did not exit. Forcing SIGKILL.", path, proc.pid)
                    if hasattr(os, "killpg") and hasattr(os, "getpgid"):
                        try:
                            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        proc.kill()
                    await proc.wait()
            except (OSError, RuntimeError) as exc:
                logger.error("Error terminating process group for '%s': %s", path, exc)

    async def _stream_logs(self, stream: asyncio.StreamReader, prefix: str):
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                decoded = line.decode("utf-8", errors="replace").rstrip()
                logger.info("[%s] %s", prefix, decoded)
        except asyncio.CancelledError:
            pass
        except (OSError, RuntimeError) as exc:
            logger.error("Error streaming logs for %s: %s", prefix, exc)

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
                _, writer = await asyncio.open_connection(host, port)
                writer.close()
                await writer.wait_closed()
                return True
            except (ConnectionRefusedError, OSError):
                remaining = deadline - loop.time()
                if remaining <= 0:
                    return False
                await asyncio.sleep(min(interval, remaining))

    async def cleanup(self):
        logger.info("Initiating cleanup of all active subprocesses.")
        paths = list(self._processes.keys())
        for path in paths:
            await self.stop_managed_server(path)

        for task in self._log_tasks:
            if not task.done():
                task.cancel()
        if self._log_tasks:
            await asyncio.gather(*self._log_tasks, return_exceptions=True)
            self._log_tasks.clear()
        logger.info("Process manager cleanup complete.")
