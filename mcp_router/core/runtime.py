from __future__ import annotations

import asyncio
import time
from dataclasses import dataclass, field
from enum import StrEnum

from .config_loader import Endpoint, ManagedEndpointConfig


class RuntimeState(StrEnum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    FAILED = "failed"


class RuntimeUnavailableError(RuntimeError):
    def __init__(self, path: str, state: RuntimeState) -> None:
        super().__init__(f"Endpoint '{path}' runtime is {state.value}")
        self.path = path
        self.state = state


@dataclass
class EndpointRuntime:
    """Mutable operational state owned by exactly one configured endpoint."""

    config: Endpoint
    state: RuntimeState
    lock: asyncio.Lock = field(default_factory=asyncio.Lock, repr=False)
    process: asyncio.subprocess.Process | None = field(default=None, repr=False)
    stdout_task: asyncio.Task[None] | None = field(default=None, repr=False)
    stderr_task: asyncio.Task[None] | None = field(default=None, repr=False)
    exit_task: asyncio.Task[None] | None = field(default=None, repr=False)
    restart_task: asyncio.Task[None] | None = field(default=None, repr=False)
    active_leases: int = 0
    last_completed_activity: float = field(default_factory=time.monotonic)
    last_exit_code: int | None = None
    failure_reason: str | None = None
    restart_attempts: int = 0
    legacy_session_ids: set[str] = field(default_factory=set, repr=False)
    legacy_tasks: set[asyncio.Task[None]] = field(default_factory=set, repr=False)
    _lease_condition: asyncio.Condition = field(default_factory=asyncio.Condition, repr=False)

    @classmethod
    def from_config(cls, config: Endpoint) -> EndpointRuntime:
        state = RuntimeState.STOPPED if isinstance(config, ManagedEndpointConfig) else RuntimeState.RUNNING
        return cls(config=config, state=state)

    @property
    def path(self) -> str:
        return self.config.path

    @property
    def managed(self) -> bool:
        return isinstance(self.config, ManagedEndpointConfig)

    async def acquire_lease(self) -> UpstreamLease:
        async with self._lease_condition:
            if self.state in {RuntimeState.DRAINING, RuntimeState.FAILED}:
                raise RuntimeUnavailableError(self.path, self.state)
            if self.managed and self.state is not RuntimeState.RUNNING:
                raise RuntimeUnavailableError(self.path, self.state)
            self.active_leases += 1
        return UpstreamLease(self)

    async def _release_lease(self) -> None:
        async with self._lease_condition:
            if self.active_leases <= 0:
                return
            self.active_leases -= 1
            self.last_completed_activity = time.monotonic()
            if self.active_leases == 0:
                self._lease_condition.notify_all()

    async def wait_for_leases(self) -> None:
        async with self._lease_condition:
            await self._lease_condition.wait_for(lambda: self.active_leases == 0)

    def track_legacy_task(self, task: asyncio.Task[None]) -> None:
        self.legacy_tasks.add(task)
        task.add_done_callback(self.legacy_tasks.discard)

    async def cancel_legacy_tasks(self) -> None:
        tasks = list(self.legacy_tasks)
        for task in tasks:
            if not task.done():
                task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        self.legacy_tasks.clear()


class UpstreamLease:
    """Idempotent lease held for the complete lifetime of one upstream operation."""

    def __init__(self, runtime: EndpointRuntime) -> None:
        self.runtime = runtime
        self._released = False
        self._release_lock = asyncio.Lock()

    async def release(self) -> None:
        async with self._release_lock:
            if self._released:
                return
            self._released = True
            await self.runtime._release_lease()
