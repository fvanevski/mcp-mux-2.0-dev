from __future__ import annotations

import asyncio
import json
import logging
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest
from starlette.middleware.errors import ServerErrorMiddleware
from starlette.requests import ClientDisconnect
from starlette.responses import StreamingResponse

from mcp_router.core.config_loader import EndpointConfig
from mcp_router.core.observability import (
    ASGIMessage,
    ASGIReceive,
    ASGISend,
    GatewayMetrics,
    GatewayObservabilityMiddleware,
)
from mcp_router.core.protocol import (
    CLIENT_CAPABILITIES_META,
    MODERN_PROTOCOL_VERSION,
    PROTOCOL_VERSION_META,
)
from mcp_router.server import app, router


def _modern_request(request_id: int, method: str, params: dict[str, object] | None = None):
    body_params = dict(params or {})
    body_params["_meta"] = {
        PROTOCOL_VERSION_META: MODERN_PROTOCOL_VERSION,
        CLIENT_CAPABILITIES_META: {},
    }
    return {
        "jsonrpc": "2.0",
        "id": request_id,
        "method": method,
        "params": body_params,
    }


@pytest.fixture(autouse=True)
def _reset_router():
    router._configs = {}
    router._legacy_bridge = None
    router._metrics.denied_calls.clear()
    router._metrics.upstream_errors.clear()
    router._metrics.stream_cancellations.clear()
    yield
    router._configs = {}
    router._legacy_bridge = None
    router._metrics.denied_calls.clear()
    router._metrics.upstream_errors.clear()
    router._metrics.stream_cancellations.clear()


def _json_stream(payload: bytes, *, status_code: int = 200):
    response = MagicMock()
    response.status_code = status_code
    response.headers = httpx.Headers({"content-type": "application/json"})
    response.aread = AsyncMock(return_value=payload)
    stream = MagicMock()
    stream.__aenter__ = AsyncMock(return_value=response)
    stream.__aexit__ = AsyncMock(return_value=None)
    return stream


@pytest.mark.asyncio
async def test_task_cancellation_records_cancelled_outcome_without_stream_disconnect_metric(caplog):
    metrics = GatewayMetrics()

    async def cancelled_app(
        scope: ASGIMessage,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        del receive, send
        scope["mcp.endpoint"] = "weather"
        raise asyncio.CancelledError

    async def receive() -> ASGIMessage:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[ASGIMessage] = []

    async def send(message: ASGIMessage) -> None:
        sent.append(message)

    middleware = GatewayObservabilityMiddleware(cancelled_app, metrics=metrics)
    scope: ASGIMessage = {
        "type": "http",
        "method": "GET",
        "path": "/weather",
        "headers": [],
    }
    caplog.set_level(logging.INFO, logger="mcp_router.requests")

    with pytest.raises(asyncio.CancelledError):
        await middleware(scope, receive, send)

    assert sent == []
    assert metrics.snapshot("weather")["stream_cancellations_total"] == 0
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "mcp_router.requests"
    ]
    assert records[-1]["endpoint"] == "weather"
    assert records[-1]["status"] == 499
    assert records[-1]["cancelled"] is True


@pytest.mark.asyncio
async def test_asgi23_stream_disconnect_message_is_counted_and_logged_once(caplog):
    metrics = GatewayMetrics()
    stream_started = asyncio.Event()
    never = asyncio.Event()

    async def body():
        yield b"first"
        await never.wait()

    async def streaming_app(
        scope: ASGIMessage,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        scope["mcp.endpoint"] = "weather"
        await StreamingResponse(body(), media_type="text/event-stream")(scope, receive, send)

    receive_count = 0

    async def receive() -> ASGIMessage:
        nonlocal receive_count
        receive_count += 1
        if receive_count == 1:
            return {"type": "http.request", "body": b"", "more_body": False}
        await stream_started.wait()
        return {"type": "http.disconnect"}

    sent: list[ASGIMessage] = []

    async def send(message: ASGIMessage) -> None:
        sent.append(message)
        if message.get("type") == "http.response.start":
            stream_started.set()

    middleware = GatewayObservabilityMiddleware(streaming_app, metrics=metrics)
    scope: ASGIMessage = {
        "type": "http",
        "method": "GET",
        "path": "/weather",
        "headers": [],
        "asgi": {"version": "3.0", "spec_version": "2.3"},
    }
    caplog.set_level(logging.INFO, logger="mcp_router.requests")

    await middleware(scope, receive, send)

    assert metrics.snapshot("weather")["stream_cancellations_total"] == 1
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "mcp_router.requests"
    ]
    assert records[-1]["endpoint"] == "weather"
    assert records[-1]["status"] == 499
    assert records[-1]["cancelled"] is True


@pytest.mark.asyncio
async def test_asgi24_failed_stream_send_is_counted_and_logged_once(caplog):
    metrics = GatewayMetrics()

    async def body():
        yield b"first"

    async def streaming_app(
        scope: ASGIMessage,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        scope["mcp.endpoint"] = "weather"
        await StreamingResponse(body(), media_type="text/event-stream")(scope, receive, send)

    async def receive() -> ASGIMessage:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[ASGIMessage] = []

    async def send(message: ASGIMessage) -> None:
        sent.append(message)
        if message.get("type") == "http.response.body" and message.get("more_body"):
            raise OSError("client disconnected")

    middleware = GatewayObservabilityMiddleware(streaming_app, metrics=metrics)
    scope: ASGIMessage = {
        "type": "http",
        "method": "GET",
        "path": "/weather",
        "headers": [],
        "asgi": {"version": "3.0", "spec_version": "2.4"},
    }
    caplog.set_level(logging.INFO, logger="mcp_router.requests")

    with pytest.raises(ClientDisconnect):
        await middleware(scope, receive, send)

    assert metrics.snapshot("weather")["stream_cancellations_total"] == 1
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "mcp_router.requests"
    ]
    assert records[-1]["endpoint"] == "weather"
    assert records[-1]["status"] == 499
    assert records[-1]["cancelled"] is True


@pytest.mark.asyncio
async def test_outer_observability_adds_request_id_to_framework_generated_500(caplog):
    metrics = GatewayMetrics()
    request_id = "req-framework-500"

    async def failing_app(
        scope: ASGIMessage,
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        del receive, send
        scope["mcp.endpoint"] = "weather"
        raise RuntimeError("boom")

    async def receive() -> ASGIMessage:
        return {"type": "http.request", "body": b"", "more_body": False}

    sent: list[ASGIMessage] = []

    async def send(message: ASGIMessage) -> None:
        sent.append(message)

    middleware = GatewayObservabilityMiddleware(
        ServerErrorMiddleware(failing_app),
        metrics=metrics,
    )
    scope: ASGIMessage = {
        "type": "http",
        "method": "GET",
        "path": "/weather",
        "headers": [(b"x-request-id", request_id.encode("ascii"))],
    }
    caplog.set_level(logging.INFO, logger="mcp_router.requests")

    with pytest.raises(RuntimeError, match="boom"):
        await middleware(scope, receive, send)

    start = next(message for message in sent if message.get("type") == "http.response.start")
    response_headers = dict(start["headers"])
    assert start["status"] == 500
    assert response_headers[b"x-request-id"] == request_id.encode("ascii")
    records = [
        json.loads(record.message)
        for record in caplog.records
        if record.name == "mcp_router.requests"
    ]
    assert records[-1]["request_id"] == request_id
    assert records[-1]["status"] == 500


def test_exported_app_places_observability_outside_starlette_error_boundary():
    assert isinstance(app, GatewayObservabilityMiddleware)
    assert app.app is router.app


@pytest.mark.asyncio
async def test_structured_request_log_contains_operational_fields_without_payload_or_credentials(caplog):
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://upstream.local/mcp",
            summary="Weather",
            transport="streamable-http",
        )
    }
    secret = "caller-secret-value"
    request_id = "req-observe-1"
    stream = _json_stream(b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}')

    caplog.set_level(logging.INFO, logger="mcp_router.requests")
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        with patch("httpx.AsyncClient.stream", return_value=stream):
            response = await client.post(
                "/weather",
                json=_modern_request(1, "tools/list"),
                headers={
                    "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                    "Mcp-Method": "tools/list",
                    "Authorization": f"Bearer {secret}",
                    "X-Request-Id": request_id,
                },
            )

    assert response.status_code == 200
    assert response.headers["x-request-id"] == request_id
    records = [json.loads(record.message) for record in caplog.records if record.name == "mcp_router.requests"]
    assert records
    record = records[-1]
    assert record["event"] == "mcp_request"
    assert record["request_id"] == request_id
    assert record["endpoint"] == "weather"
    assert record["protocol_revision"] == MODERN_PROTOCOL_VERSION
    assert record["method"] == "tools/list"
    assert record["status"] == 200
    assert record["duration_ms"] >= 0
    assert record["bytes_streamed"] > 0
    assert record["policy_outcome"] == "allowed"
    rendered = "\n".join(item.message for item in caplog.records)
    assert secret not in rendered
    assert "caller-secret-value" not in rendered
    assert "params" not in record


@pytest.mark.asyncio
async def test_valid_trace_context_is_forwarded_and_invalid_context_is_dropped():
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://upstream.local/mcp",
            summary="Weather",
            transport="streamable-http",
        )
    }
    valid_traceparent = "00-4bf92f3577b34da6a3ce929d0e0e4736-00f067aa0ba902b7-01"

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        stream = _json_stream(b'{"jsonrpc":"2.0","id":1,"result":{"tools":[]}}')
        with patch("httpx.AsyncClient.stream", return_value=stream) as call:
            accepted = await client.post(
                "/weather",
                json=_modern_request(1, "tools/list"),
                headers={
                    "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                    "Mcp-Method": "tools/list",
                    "traceparent": valid_traceparent,
                    "tracestate": "vendor=value",
                    "baggage": "secret-shaped=caller-controlled",
                },
            )
        forwarded = {key.casefold(): value for key, value in call.call_args.kwargs["headers"].items()}
        assert forwarded["traceparent"] == valid_traceparent
        assert forwarded["tracestate"] == "vendor=value"
        assert "baggage" not in forwarded
        assert accepted.status_code == 200

        stream = _json_stream(b'{"jsonrpc":"2.0","id":2,"result":{"tools":[]}}')
        with patch("httpx.AsyncClient.stream", return_value=stream) as call:
            rejected_context = await client.post(
                "/weather",
                json=_modern_request(2, "tools/list"),
                headers={
                    "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                    "Mcp-Method": "tools/list",
                    "traceparent": "00-not-a-valid-trace-parent",
                    "tracestate": "must=not-forward",
                },
            )
        forwarded = {key.casefold(): value for key, value in call.call_args.kwargs["headers"].items()}
        assert "traceparent" not in forwarded
        assert "tracestate" not in forwarded
        assert rejected_context.status_code == 200

        stream = _json_stream(b'{"jsonrpc":"2.0","id":3,"result":{"tools":[]}}')
        with patch("httpx.AsyncClient.stream", return_value=stream) as call:
            uppercase_context = await client.post(
                "/weather",
                json=_modern_request(3, "tools/list"),
                headers={
                    "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                    "Mcp-Method": "tools/list",
                    "traceparent": "00-4BF92F3577B34DA6A3CE929D0E0E4736-00F067AA0BA902B7-01",
                    "tracestate": "must=not-forward",
                },
            )
        forwarded = {key.casefold(): value for key, value in call.call_args.kwargs["headers"].items()}
        assert "traceparent" not in forwarded
        assert "tracestate" not in forwarded
        assert uppercase_context.status_code == 200


@pytest.mark.asyncio
async def test_denied_calls_and_upstream_failures_are_exposed_as_metrics():
    router._configs = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://upstream.local/mcp",
            summary="Weather",
            transport="streamable-http",
            denied_tools=["dangerous"],
        )
    }
    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=app),
        base_url="http://localhost",
    ) as client:
        with patch("httpx.AsyncClient.stream") as upstream:
            denied = await client.post(
                "/weather",
                json=_modern_request(1, "tools/call", {"name": "dangerous", "arguments": {}}),
                headers={
                    "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                    "Mcp-Method": "tools/call",
                    "Mcp-Name": "dangerous",
                },
            )
        assert denied.status_code == 403
        upstream.assert_not_called()

        router._configs["weather"] = EndpointConfig(
            path="weather",
            mode="remote",
            url="http://upstream.local/mcp",
            summary="Weather",
            transport="streamable-http",
        )
        stream = _json_stream(
            b'{"jsonrpc":"2.0","id":2,"error":{"code":-32000,"message":"failure"}}',
            status_code=503,
        )
        with patch("httpx.AsyncClient.stream", return_value=stream):
            failed = await client.post(
                "/weather",
                json=_modern_request(2, "tools/list"),
                headers={
                    "MCP-Protocol-Version": MODERN_PROTOCOL_VERSION,
                    "Mcp-Method": "tools/list",
                },
            )
        assert failed.status_code == 503

        metrics = (await client.get("/metrics")).json()["endpoints"][0]

    assert metrics["denied_calls_total"] == 1
    assert metrics["upstream_errors_total"] == 1
    assert metrics["stream_cancellations_total"] == 0
    assert metrics["process_restarts_total"] == 0
    assert metrics["active_upstream_leases"] == 0
