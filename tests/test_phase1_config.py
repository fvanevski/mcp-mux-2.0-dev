from __future__ import annotations

from pathlib import Path

import pytest
from pydantic import ValidationError

from mcp_router import server as server_module
from mcp_router.core.config_loader import (
    ConfigWatcher,
    EndpointConfig,
    ManagedEndpointConfig,
    RouterConfig,
    load_router_config,
)


@pytest.mark.parametrize("path", ["summary", "SUMMARY", "nested/path", "bad path", ".hidden"])
def test_reserved_or_malformed_route_names_are_rejected(path: str):
    with pytest.raises(ValidationError):
        EndpointConfig(
            path=path,
            mode="remote",
            url="https://example.test/mcp",
            summary="Invalid route",
        )


@pytest.mark.parametrize("mode", ["stdio_bridge", "unknown"])
def test_unimplemented_or_unknown_modes_are_rejected(mode: str):
    with pytest.raises(ValidationError):
        RouterConfig.model_validate(
            {
                "endpoints": [
                    {
                        "path": "example",
                        "mode": mode,
                        "url": "https://example.test/mcp",
                        "summary": "Unsupported mode",
                    }
                ]
            }
        )


def test_managed_cli_rejects_legacy_command_field():
    with pytest.raises(ValidationError):
        RouterConfig.model_validate(
            {
                "endpoints": [
                    {
                        "path": "managed",
                        "mode": "managed_cli",
                        "url": "http://localhost:3033/mcp",
                        "summary": "Managed endpoint",
                        "command": "example --serve",
                    }
                ]
            }
        )


@pytest.mark.parametrize(
    "execution_fields",
    [
        {},
        {"argv": ["example", "--serve"], "unsafe_shell_command": "example --serve"},
    ],
)
def test_managed_cli_requires_exactly_one_execution_source(execution_fields: dict[str, object]):
    with pytest.raises(ValidationError, match="exactly one of argv or unsafe_shell_command"):
        RouterConfig.model_validate(
            {
                "endpoints": [
                    {
                        "path": "managed",
                        "mode": "managed_cli",
                        "url": "http://localhost:3033/mcp",
                        "summary": "Managed endpoint",
                        **execution_fields,
                    }
                ]
            }
        )


def test_managed_cli_validates_structured_execution_and_readiness():
    config = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "managed",
                    "mode": "managed_cli",
                    "url": "http://localhost:3033/mcp",
                    "summary": "Managed endpoint",
                    "argv": ["example", "--serve"],
                    "env": {"EXAMPLE_MODE": "test"},
                    "cwd": "/tmp",
                    "readiness": {"timeout": 7.5, "interval": 0.1},
                }
            ]
        }
    )

    endpoint = config.endpoints[0]
    assert isinstance(endpoint, ManagedEndpointConfig)
    assert endpoint.argv == ["example", "--serve"]
    assert endpoint.env == {"EXAMPLE_MODE": "test"}
    assert endpoint.cwd == "/tmp"
    assert endpoint.readiness.host == "localhost"
    assert endpoint.readiness.port == 3033
    assert endpoint.readiness.timeout == 7.5
    assert endpoint.readiness.interval == 0.1


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("url", "ftp://example.test/mcp"),
        ("timeout", 0),
        ("transport", "websocket"),
        ("allowed_tools", ["tool", "tool"]),
    ],
)
def test_endpoint_policy_and_transport_fields_are_validated(field: str, value: object):
    endpoint = {
        "path": "example",
        "mode": "remote",
        "url": "https://example.test/mcp",
        "summary": "Example",
        field: value,
    }
    with pytest.raises(ValidationError):
        RouterConfig.model_validate({"endpoints": [endpoint]})


def test_repository_config_migrates_and_validates():
    config_path = Path(__file__).parent / "fixtures" / "compatibility-baseline-config.yaml"
    config = load_router_config(str(config_path))

    assert {endpoint.mode for endpoint in config.endpoints} == {"remote", "managed_cli"}
    managed = next(endpoint for endpoint in config.endpoints if endpoint.mode == "managed_cli")
    assert isinstance(managed, ManagedEndpointConfig)
    assert managed.unsafe_shell_command is not None
    assert managed.argv is None
    assert managed.readiness.host == "localhost"
    assert managed.readiness.port == 3033


@pytest.mark.asyncio
async def test_invalid_reload_preserves_last_known_good_config(tmp_path: Path):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """endpoints:\n  - path: good\n    mode: remote\n    url: https://example.test/mcp\n    summary: Good\n""",
        encoding="utf-8",
    )
    applied: list[RouterConfig] = []
    watcher = ConfigWatcher(str(config_path), applied.append)

    assert await watcher._reload_if_changed() is True
    assert [endpoint.path for endpoint in applied[-1].endpoints] == ["good"]

    config_path.write_text(
        """endpoints:\n  - path: summary\n    mode: remote\n    url: https://example.test/mcp\n    summary: Invalid\n""",
        encoding="utf-8",
    )
    watcher._last_mtime = None

    assert await watcher._reload_if_changed() is False
    assert len(applied) == 1
    assert [endpoint.path for endpoint in applied[-1].endpoints] == ["good"]


@pytest.mark.asyncio
async def test_invalid_initial_config_prevents_runtime_startup(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        """endpoints:\n  - path: summary\n    mode: remote\n    url: https://example.test/mcp\n    summary: Invalid\n""",
        encoding="utf-8",
    )
    monkeypatch.setattr(server_module, "CONFIG_PATH", str(config_path))
    server_module.router._running = False

    with pytest.raises(ValidationError):
        async with server_module.lifespan(server_module.app):
            pass

    assert server_module.router._running is False
