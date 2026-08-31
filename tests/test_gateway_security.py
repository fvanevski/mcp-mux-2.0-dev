from __future__ import annotations

from collections.abc import MutableMapping
from typing import Any

import httpx
import pytest
from pydantic import SecretStr
from starlette.responses import JSONResponse

from mcp_router.core.config_loader import SecurityConfig
from mcp_router.core.security import (
    ASGIApp,
    ASGIReceive,
    ASGISend,
    GatewaySecurityMiddleware,
)


async def _ok_app(
    scope: MutableMapping[str, Any],
    receive: ASGIReceive,
    send: ASGISend,
) -> None:
    response = JSONResponse({"ok": True})
    await response(scope, receive, send)


def _app(security: SecurityConfig) -> ASGIApp:
    return GatewaySecurityMiddleware(_ok_app, get_config=lambda: security)


@pytest.mark.asyncio
async def test_local_only_accepts_loopback_origin_with_port() -> None:
    transport = httpx.ASGITransport(
        app=_app(SecurityConfig()),
        client=("127.0.0.1", 43123),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8012",
    ) as client:
        response = await client.get(
            "/mcp",
            headers={"Origin": "http://127.0.0.1:8012"},
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == "http://127.0.0.1:8012"


@pytest.mark.asyncio
async def test_local_only_rejects_non_loopback_origin() -> None:
    transport = httpx.ASGITransport(
        app=_app(SecurityConfig()),
        client=("127.0.0.1", 43123),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8012",
    ) as client:
        response = await client.get(
            "/mcp",
            headers={"Origin": "http://evil.example.com"},
        )

    assert response.status_code == 403
    assert response.json() == {"error": "Invalid Origin"}


@pytest.mark.asyncio
async def test_local_only_rejects_non_loopback_peer() -> None:
    transport = httpx.ASGITransport(
        app=_app(SecurityConfig()),
        client=("203.0.113.9", 43123),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8012",
    ) as client:
        response = await client.get("/mcp")

    assert response.status_code == 403
    assert response.json() == {"error": "Local-only gateway access required"}


@pytest.mark.asyncio
async def test_remote_accepts_non_loopback_peer_without_global_auth() -> None:
    security = SecurityConfig(
        mode="remote",
        allowed_hosts=["mcp.example.test"],
        allowed_origins=[],
    )
    transport = httpx.ASGITransport(
        app=_app(security),
        client=("203.0.113.9", 43123),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://mcp.example.test",
    ) as client:
        response = await client.get("/mcp")

    assert response.status_code == 200
    assert response.json() == {"ok": True}


@pytest.mark.asyncio
async def test_remote_enforces_host_and_origin_allowlists() -> None:
    security = SecurityConfig(
        mode="remote",
        allowed_hosts=["mcp.example.test"],
        allowed_origins=["https://agent.example.test"],
    )
    transport = httpx.ASGITransport(
        app=_app(security),
        client=("203.0.113.9", 43123),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://mcp.example.test",
    ) as client:
        allowed = await client.get(
            "/mcp",
            headers={"Origin": "https://agent.example.test"},
        )
        invalid_origin = await client.get(
            "/mcp",
            headers={"Origin": "https://evil.example.test"},
        )
        invalid_host = await client.get(
            "http://evil.example.test/mcp",
            headers={"Origin": "https://agent.example.test"},
        )

    assert allowed.status_code == 200
    assert invalid_origin.status_code == 403
    assert invalid_origin.json() == {"error": "Invalid Origin"}
    assert invalid_host.status_code == 403
    assert invalid_host.json() == {"error": "Invalid Host"}


def test_unknown_security_mode_is_rejected() -> None:
    with pytest.raises(ValueError):
        SecurityConfig(mode="anonymous")  # type: ignore[arg-type]


@pytest.mark.asyncio
async def test_authenticated_mode_does_not_implicitly_allow_loopback_origin() -> None:
    security = SecurityConfig(
        mode="authenticated",
        api_key=SecretStr("test-key"),
        allowed_hosts=["127.0.0.1"],
        allowed_origins=["http://trusted.example"],
    )
    transport = httpx.ASGITransport(
        app=_app(security),
        client=("127.0.0.1", 43123),
    )
    async with httpx.AsyncClient(
        transport=transport,
        base_url="http://127.0.0.1:8012",
    ) as client:
        response = await client.get(
            "/mcp",
            headers={
                "Origin": "http://127.0.0.1:8012",
                "Authorization": "Bearer test-key",
            },
        )

    assert response.status_code == 403
    assert response.json() == {"error": "Invalid Origin"}
