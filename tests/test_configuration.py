from __future__ import annotations

import pytest

from mcp_router.core.config_loader import (
    EndpointConfig,
    ManagedEndpointConfig,
    RouterConfig,
    expand_env_vars,
)


def test_valid_config():
    data = {
        "endpoints": [
            {
                "path": "weather",
                "mode": "remote",
                "url": "http://api.weather.com",
                "summary": "Weather API",
            },
            {
                "path": "files",
                "mode": "managed_cli",
                "argv": ["uvx", "mcp-server-filesystem"],
                "url": "http://localhost:8011/mcp",
                "summary": "File tools",
            },
        ]
    }
    cfg = RouterConfig.model_validate(data)
    assert len(cfg.endpoints) == 2
    assert cfg.endpoints[0].path == "weather"
    assert isinstance(cfg.endpoints[1], ManagedEndpointConfig)
    assert cfg.endpoints[1].url == "http://localhost:8011/mcp"


def test_config_port_collision():
    data = {
        "endpoints": [
            {
                "path": "files1",
                "mode": "managed_cli",
                "argv": ["uvx", "mcp-server-filesystem"],
                "url": "http://localhost:8011/mcp",
                "summary": "Files 1",
            },
            {
                "path": "files2",
                "mode": "managed_cli",
                "argv": ["uvx", "mcp-server-filesystem"],
                "url": "http://localhost:8011/mcp",
                "summary": "Files 2",
            },
        ]
    }
    with pytest.raises(ValueError, match="Duplicate port detected"):
        RouterConfig.model_validate(data)


def test_config_duplicate_path():
    data = {
        "endpoints": [
            {
                "path": "weather",
                "mode": "remote",
                "url": "http://api1.weather.com",
                "summary": "Weather 1",
            },
            {
                "path": "weather",
                "mode": "remote",
                "url": "http://api2.weather.com",
                "summary": "Weather 2",
            },
        ]
    }
    with pytest.raises(ValueError, match="Duplicate path detected"):
        RouterConfig.model_validate(data)


def test_config_missing_remote_url():
    with pytest.raises(ValueError):
        RouterConfig.model_validate(
            {"endpoints": [{"path": "weather", "mode": "remote", "summary": "Missing URL"}]}
        )


def test_config_missing_managed_cli_url():
    with pytest.raises(ValueError):
        RouterConfig.model_validate(
            {
                "endpoints": [
                    {
                        "path": "files",
                        "mode": "managed_cli",
                        "argv": ["uvx"],
                        "summary": "Missing URL",
                    }
                ]
            }
        )


def test_config_expands_environment_variables(monkeypatch):
    monkeypatch.setenv("TEST_HF_TOKEN", "hf_test_token")
    data = {
        "endpoints": [
            {
                "path": "huggingface",
                "mode": "remote",
                "url": "https://huggingface.co/mcp",
                "summary": "HuggingFace API",
                "headers": {
                    "Authorization": "Bearer ${TEST_HF_TOKEN}",
                    "X-Default": "${MISSING_HEADER:-fallback}",
                },
            }
        ]
    }
    cfg = RouterConfig.model_validate(expand_env_vars(data))
    assert cfg.endpoints[0].headers == {
        "Authorization": "Bearer hf_test_token",
        "X-Default": "fallback",
    }


def test_config_env_expansion_requires_missing_variables(monkeypatch):
    monkeypatch.delenv("MISSING_HF_TOKEN", raising=False)
    with pytest.raises(ValueError, match="MISSING_HF_TOKEN"):
        expand_env_vars({"headers": {"Authorization": "Bearer ${MISSING_HF_TOKEN}"}})


def test_config_omits_empty_authorization_header(monkeypatch):
    monkeypatch.delenv("OPTIONAL_HF_TOKEN", raising=False)
    data = {
        "endpoints": [
            {
                "path": "huggingface",
                "mode": "remote",
                "url": "https://huggingface.co/mcp",
                "summary": "HuggingFace API",
                "headers": {"Authorization": "Bearer ${OPTIONAL_HF_TOKEN:-}"},
            }
        ]
    }
    cfg = RouterConfig.model_validate(expand_env_vars(data))
    assert cfg.endpoints[0].headers is None


def test_config_normalizes_duplicate_bearer_authorization(monkeypatch):
    monkeypatch.setenv("HF_TOKEN_WITH_SCHEME", "Bearer hf_test_token")
    data = {
        "endpoints": [
            {
                "path": "huggingface",
                "mode": "remote",
                "url": "https://huggingface.co/mcp",
                "summary": "HuggingFace API",
                "headers": {"Authorization": "Bearer ${HF_TOKEN_WITH_SCHEME}"},
            }
        ]
    }
    cfg = RouterConfig.model_validate(expand_env_vars(data))
    assert cfg.endpoints[0].headers == {"Authorization": "Bearer hf_test_token"}


def test_config_with_headers():
    cfg = RouterConfig.model_validate(
        {
            "endpoints": [
                {
                    "path": "weather",
                    "mode": "remote",
                    "url": "http://api.weather.com",
                    "summary": "Weather API",
                    "headers": {
                        "Authorization": "Bearer mytoken",
                        "X-Custom-Header": "value",
                    },
                }
            ]
        }
    )
    assert cfg.endpoints[0].headers == {
        "Authorization": "Bearer mytoken",
        "X-Custom-Header": "value",
    }
