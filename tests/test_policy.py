from __future__ import annotations

import json

import pytest

from mcp_router.core.config_loader import EndpointConfig
from mcp_router.server import filter_tools_response


def test_endpoint_config_allowed_denied_validation():
    with pytest.raises(ValueError, match="mutually exclusive"):
        EndpointConfig(
            path="test",
            mode="remote",
            url="http://localhost/mcp",
            summary="Test",
            allowed_tools=["toolA"],
            denied_tools=["toolB"],
        )

    cfg_allow = EndpointConfig(
        path="test",
        mode="remote",
        url="http://localhost/mcp",
        summary="Test",
        allowed_tools=["toolA"],
    )
    assert cfg_allow.allowed_tools == ["toolA"]
    assert cfg_allow.denied_tools is None

    cfg_deny = EndpointConfig(
        path="test",
        mode="remote",
        url="http://localhost/mcp",
        summary="Test",
        denied_tools=["toolB"],
    )
    assert cfg_deny.allowed_tools is None
    assert cfg_deny.denied_tools == ["toolB"]


def test_filter_tools_response_sse():
    line = 'data: {"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"toolA"},{"name":"toolB"}]}}'
    filtered = filter_tools_response(line, ["toolA"], None)
    assert filtered.startswith("data: ")
    data = json.loads(filtered[6:])
    assert [tool["name"] for tool in data["result"]["tools"]] == ["toolA"]


def test_filter_tools_response_json():
    body = '{"jsonrpc":"2.0","id":1,"result":{"tools":[{"name":"toolA"},{"name":"toolB"}]}}'
    filtered = filter_tools_response(body, None, ["toolA"])
    data = json.loads(filtered)
    assert [tool["name"] for tool in data["result"]["tools"]] == ["toolB"]
