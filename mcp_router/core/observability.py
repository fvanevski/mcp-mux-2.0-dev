from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import uuid
from collections import Counter
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from dataclasses import dataclass, field
from typing import Any

ASGIMessage = MutableMapping[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[MutableMapping[str, Any], ASGIReceive, ASGISend], Awaitable[None]]

logger = logging.getLogger("mcp_router.requests")
_REQUEST_ID_RE = re.compile(r"^[A-Za-z0-9._:-]{1,128}$")


def _header(scope: Mapping[str, Any], name: str) -> str | None:
    target = name.casefold().encode("latin-1")
    for key, value in scope.get("headers", []):
        if key.casefold() == target:
            return value.decode("latin-1")
    return None


def _request_id(scope: Mapping[str, Any]) -> str:
    supplied = _header(scope, "x-request-id")
    if supplied is not None and _REQUEST_ID_RE.fullmatch(supplied):
        return supplied
    return uuid.uuid4().hex


@dataclass
class GatewayMetrics:
    """Small in-process counters for gateway outcomes not already owned by endpoint runtime state."""

    denied_calls: Counter[str] = field(default_factory=Counter)
    upstream_errors: Counter[str] = field(default_factory=Counter)
    stream_cancellations: Counter[str] = field(default_factory=Counter)

    def record_denied_call(self, endpoint: str) -> None:
        self.denied_calls[endpoint] += 1

    def record_upstream_error(self, endpoint: str) -> None:
        self.upstream_errors[endpoint] += 1

    def record_stream_cancellation(self, endpoint: str) -> None:
        self.stream_cancellations[endpoint] += 1

    def snapshot(self, endpoint: str) -> dict[str, int]:
        return {
            "denied_calls_total": self.denied_calls[endpoint],
            "upstream_errors_total": self.upstream_errors[endpoint],
            "stream_cancellations_total": self.stream_cancellations[endpoint],
        }


class GatewayObservabilityMiddleware:
    """Emit one payload-free structured request record after the complete ASGI response lifetime."""

    def __init__(self, app: ASGIApp, *, metrics: GatewayMetrics) -> None:
        self.app = app
        self.metrics = metrics

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        started = time.perf_counter()
        request_id = _request_id(scope)
        scope["mcp.request_id"] = request_id
        status_code = 500
        bytes_streamed = 0
        cancelled = False

        async def observed_send(message: ASGIMessage) -> None:
            nonlocal status_code, bytes_streamed
            message_type = message.get("type")
            if message_type == "http.response.start":
                status_code = int(message.get("status", 500))
                raw_headers = list(message.get("headers", []))
                if not any(key.casefold() == b"x-request-id" for key, _ in raw_headers):
                    raw_headers.append((b"x-request-id", request_id.encode("ascii")))
                message["headers"] = raw_headers
            elif message_type == "http.response.body":
                body = message.get("body", b"")
                if isinstance(body, (bytes, bytearray, memoryview)):
                    bytes_streamed += len(body)
            await send(message)

        try:
            await self.app(scope, receive, observed_send)
        except asyncio.CancelledError:
            cancelled = True
            status_code = 499
            endpoint = scope.get("mcp.endpoint")
            if isinstance(endpoint, str) and endpoint:
                self.metrics.record_stream_cancellation(endpoint)
            raise
        finally:
            duration_ms = round((time.perf_counter() - started) * 1000, 3)
            record = {
                "event": "mcp_request",
                "request_id": request_id,
                "endpoint": scope.get("mcp.endpoint"),
                "protocol_revision": scope.get("mcp.protocol_revision"),
                "method": scope.get("mcp.method"),
                "capability": scope.get("mcp.capability"),
                "status": status_code,
                "duration_ms": duration_ms,
                "bytes_streamed": bytes_streamed,
                "policy_outcome": scope.get("mcp.policy_outcome", "not_evaluated"),
                "trace_context": bool(_header(scope, "traceparent")),
                "cancelled": cancelled,
            }
            logger.info(json.dumps(record, separators=(",", ":"), sort_keys=True))
