from __future__ import annotations

import httpx
import pytest
from pydantic import SecretStr
from starlette.responses import JSONResponse

from mcp_router.core.config_loader import SecurityConfig
from mcp_router.core.security import ASGIApp, ASGIReceive, ASGISend, GatewaySecurityMiddleware


async def _ok_app(scope: dict, receive: ASGIReceive, send: ASGISend) -> None:
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
