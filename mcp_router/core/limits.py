from __future__ import annotations

import asyncio
import time
from collections import deque
from dataclasses import dataclass, field

from mcp_router.core.config_loader import Endpoint, RequestLimitConfig


@dataclass
class _Bucket:
    active: int = 0
    timestamps: deque[float] = field(default_factory=deque)


@dataclass(frozen=True)
class LimitRejection:
    scope: str
    reason: str


class LimitLease:
    def __init__(self, limiter: RequestLimiter, keys: tuple[str, ...]) -> None:
        self._limiter = limiter
        self._keys = keys
        self._released = False

    async def release(self) -> None:
        if self._released:
            return
        self._released = True
        await self._limiter._release(self._keys)


class RequestLimiter:
    """In-process endpoint/capability concurrency and one-minute rate limiter."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._buckets: dict[str, _Bucket] = {}

    async def acquire(
        self,
        endpoint: Endpoint,
        *,
        capability_name: str | None = None,
    ) -> tuple[LimitLease | None, LimitRejection | None]:
        specs: list[tuple[str, RequestLimitConfig]] = [
            (f"endpoint:{endpoint.path}", endpoint.limits),
        ]
        if capability_name is not None:
            capability_limits = endpoint.tool_limits.get(capability_name)
            if capability_limits is not None:
                specs.append((f"tool:{endpoint.path}:{capability_name}", capability_limits))

        now = time.monotonic()
        async with self._lock:
            active_specs = [
                (key, spec)
                for key, spec in specs
                if spec.max_concurrent is not None or spec.requests_per_minute is not None
            ]
            if not active_specs:
                return None, None

            for key, spec in active_specs:
                bucket = self._buckets.setdefault(key, _Bucket())
                cutoff = now - 60.0
                while bucket.timestamps and bucket.timestamps[0] <= cutoff:
                    bucket.timestamps.popleft()
                if spec.max_concurrent is not None and bucket.active >= spec.max_concurrent:
                    return None, LimitRejection(key, "concurrency limit exceeded")
                if spec.requests_per_minute is not None and len(bucket.timestamps) >= spec.requests_per_minute:
                    return None, LimitRejection(key, "rate limit exceeded")

            acquired: list[str] = []
            for key, spec in active_specs:
                bucket = self._buckets.setdefault(key, _Bucket())
                if spec.max_concurrent is not None:
                    bucket.active += 1
                    acquired.append(key)
                if spec.requests_per_minute is not None:
                    bucket.timestamps.append(now)
            return LimitLease(self, tuple(acquired)), None

    async def _release(self, keys: tuple[str, ...]) -> None:
        if not keys:
            return
        async with self._lock:
            for key in keys:
                bucket = self._buckets.get(key)
                if bucket is not None and bucket.active > 0:
                    bucket.active -= 1
