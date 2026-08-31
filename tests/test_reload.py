from __future__ import annotations

import pytest

from mcp_router.core.config_loader import EndpointConfig, RouterConfig
from mcp_router.server import router


@pytest.mark.asyncio
async def test_apply_configuration_drops_sessions_for_removed_or_changed_endpoint():
    endpoints = {
        "weather": EndpointConfig(
            path="weather",
            mode="remote",
            url="http://api.weather.com/mcp",
            summary="Weather summary",
            transport="streamable-http",
            legacy_sse_bridge={},
        ),
        "huggingface": EndpointConfig(
            path="huggingface",
            mode="remote",
            url="https://huggingface.co/mcp",
            summary="HuggingFace remote server",
            transport="streamable-http",
            legacy_sse_bridge={},
        ),
        "files": EndpointConfig(
            path="files",
            mode="remote",
            url="http://files.example.com/mcp",
            summary="Files remote server",
            transport="streamable-http",
            legacy_sse_bridge={},
        ),
    }
    router._configs = endpoints
    router._legacy_bridge = None
    bridge = router._get_legacy_bridge()
    session_ids: dict[str, str] = {}
    for path, endpoint in endpoints.items():
        bridge.open_session(endpoint=endpoint, runtime=router._runtimes[path])
        session = next(session for session in bridge.sessions.values() if session.path_prefix == path)
        session_ids[path] = session.session_id

    new_config = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "weather",
                    "mode": "remote",
                    "url": "http://api.weather.com/v2/mcp",
                    "summary": "Updated weather summary",
                    "legacy_sse_bridge": {},
                },
                {
                    "path": "huggingface",
                    "mode": "remote",
                    "url": "https://huggingface.co/mcp",
                    "summary": "HuggingFace remote server",
                    "legacy_sse_bridge": {},
                },
            ]
        }
    )
    await router.apply_configuration(new_config)
    assert session_ids["weather"] not in bridge.sessions
    assert session_ids["files"] not in bridge.sessions
    assert session_ids["huggingface"] in bridge.sessions
    await bridge.close_all()
