from __future__ import annotations

import base64
import binascii
import json
from collections.abc import Mapping
from dataclasses import dataclass
from enum import StrEnum
from typing import Any

MODERN_PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"

HEADER_MISMATCH = -32020
UNSUPPORTED_PROTOCOL_VERSION = -32022
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
INVALID_PARAMS = -32602

_NAMED_METHOD_FIELDS = {
    "tools/call": "name",
    "prompts/get": "name",
    "resources/read": "uri",
}
_BASE64_PREFIX = "=?base64?"
_BASE64_SUFFIX = "?="


class ProtocolEra(StrEnum):
    MODERN = "modern"
    LEGACY = "legacy"


@dataclass(frozen=True)
class ParsedJSONRPCRequest:
    payload: dict[str, Any]
    method: str
    params: dict[str, Any]
    request_id: str | int | None


class ProtocolRequestError(ValueError):
    def __init__(
        self,
        code: int,
        message: str,
        *,
        status_code: int = 400,
        request_id: str | int | None = None,
        data: Any | None = None,
    ) -> None:
        super().__init__(message)
        self.code = code
        self.message = message
        self.status_code = status_code
        self.request_id = request_id
        self.data = data


def build_jsonrpc_error(
    code: int,
    message: str,
    *,
    request_id: str | int | None = None,
    data: Any | None = None,
) -> dict[str, Any]:
    error: dict[str, Any] = {"code": code, "message": message}
    if data is not None:
        error["data"] = data
    return {"jsonrpc": "2.0", "id": request_id, "error": error}


def parse_jsonrpc_request(body: bytes) -> ParsedJSONRPCRequest:
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError as exc:
        raise ProtocolRequestError(PARSE_ERROR, "Request body is not valid UTF-8 JSON") from exc

    try:
        payload = json.loads(text)
    except json.JSONDecodeError as exc:
        raise ProtocolRequestError(PARSE_ERROR, "Parse error") from exc

    if isinstance(payload, list):
        raise ProtocolRequestError(INVALID_REQUEST, "JSON-RPC batches are not supported")
    if not isinstance(payload, dict):
        raise ProtocolRequestError(INVALID_REQUEST, "JSON-RPC body must be an object")

    request_id = _request_id(payload.get("id")) if "id" in payload else None

    if payload.get("jsonrpc") != "2.0":
        raise ProtocolRequestError(
            INVALID_REQUEST,
            "JSON-RPC request must include jsonrpc='2.0'",
            request_id=request_id,
        )

    method = payload.get("method")
    if not isinstance(method, str) or not method:
        raise ProtocolRequestError(
            INVALID_REQUEST,
            "JSON-RPC request must include a non-empty method",
            request_id=request_id,
        )

    raw_params = payload.get("params", {})
    if not isinstance(raw_params, dict):
        raise ProtocolRequestError(
            INVALID_PARAMS,
            "JSON-RPC params must be an object when present",
            request_id=request_id,
        )

    return ParsedJSONRPCRequest(
        payload=payload,
        method=method,
        params=raw_params,
        request_id=request_id,
    )


def classify_protocol_era(
    request: ParsedJSONRPCRequest,
    headers: Mapping[str, str],
) -> ProtocolEra:
    meta = request.params.get("_meta")
    if isinstance(meta, dict) and PROTOCOL_VERSION_META in meta:
        return ProtocolEra.MODERN
    protocol_header = _header(headers, "MCP-Protocol-Version")
    if protocol_header == MODERN_PROTOCOL_VERSION:
        return ProtocolEra.MODERN
    if _header(headers, "Mcp-Method") is not None or _header(headers, "Mcp-Name") is not None:
        return ProtocolEra.MODERN
    return ProtocolEra.LEGACY


def validate_protocol_request(
    request: ParsedJSONRPCRequest,
    headers: Mapping[str, str],
) -> ProtocolEra:
    era = classify_protocol_era(request, headers)
    if era is ProtocolEra.LEGACY:
        return era

    meta = request.params.get("_meta")
    if not isinstance(meta, dict):
        raise ProtocolRequestError(
            INVALID_PARAMS,
            "Modern requests require params._meta",
            request_id=request.request_id,
        )

    body_version = meta.get(PROTOCOL_VERSION_META)
    if not isinstance(body_version, str) or not body_version:
        raise ProtocolRequestError(
            INVALID_PARAMS,
            f"Modern requests require _meta['{PROTOCOL_VERSION_META}']",
            request_id=request.request_id,
        )

    capabilities = meta.get(CLIENT_CAPABILITIES_META)
    if not isinstance(capabilities, dict):
        raise ProtocolRequestError(
            INVALID_PARAMS,
            f"Modern requests require object _meta['{CLIENT_CAPABILITIES_META}']",
            request_id=request.request_id,
        )

    protocol_header = _required_header(headers, "MCP-Protocol-Version", request.request_id)
    _validate_ascii_header(protocol_header, "MCP-Protocol-Version", request.request_id)
    if protocol_header != body_version:
        raise _header_mismatch(
            f"MCP-Protocol-Version header value {protocol_header!r} does not match body value {body_version!r}",
            request.request_id,
        )
    if body_version != MODERN_PROTOCOL_VERSION:
        raise ProtocolRequestError(
            UNSUPPORTED_PROTOCOL_VERSION,
            f"Unsupported MCP protocol version: {body_version}",
            request_id=request.request_id,
            data={"supportedVersions": [MODERN_PROTOCOL_VERSION]},
        )

    method_header = _required_header(headers, "Mcp-Method", request.request_id)
    _validate_ascii_header(method_header, "Mcp-Method", request.request_id)
    if method_header != request.method:
        raise _header_mismatch(
            f"Mcp-Method header value {method_header!r} does not match body value {request.method!r}",
            request.request_id,
        )

    body_name = extract_request_name(request)
    if request.method in _NAMED_METHOD_FIELDS:
        if body_name is None:
            name_field = _NAMED_METHOD_FIELDS[request.method]
            raise ProtocolRequestError(
                INVALID_PARAMS,
                f"{request.method} requires string params.{name_field}",
                request_id=request.request_id,
            )
        encoded_name = _required_header(headers, "Mcp-Name", request.request_id)
        decoded_name = _decode_mcp_header(encoded_name, "Mcp-Name", request.request_id)
        if decoded_name != body_name:
            raise _header_mismatch(
                f"Mcp-Name header value {decoded_name!r} does not match body value {body_name!r}",
                request.request_id,
            )

    return era


def extract_request_name(request: ParsedJSONRPCRequest) -> str | None:
    name_field = _NAMED_METHOD_FIELDS.get(request.method)
    if name_field is None:
        return None
    value = request.params.get(name_field)
    return value if isinstance(value, str) and value else None


def _request_id(value: Any) -> str | int:
    if value is None or isinstance(value, bool) or not isinstance(value, (str, int)):
        raise ProtocolRequestError(INVALID_REQUEST, "JSON-RPC request id must be a string or integer")
    return value


def _header(headers: Mapping[str, str], name: str) -> str | None:
    direct = headers.get(name)
    if direct is not None:
        return direct
    name_folded = name.casefold()
    for key, value in headers.items():
        if key.casefold() == name_folded:
            return value
    return None


def _required_header(
    headers: Mapping[str, str],
    name: str,
    request_id: str | int | None,
) -> str:
    value = _header(headers, name)
    if value is None or value == "":
        raise _header_mismatch(f"Required {name} header is missing", request_id)
    return value


def _validate_ascii_header(
    value: str,
    name: str,
    request_id: str | int | None,
) -> None:
    if value != value.strip() or any(char != "\t" and not (0x20 <= ord(char) <= 0x7E) for char in value):
        raise _header_mismatch(f"{name} header contains invalid characters", request_id)


def _decode_mcp_header(
    value: str,
    name: str,
    request_id: str | int | None,
) -> str:
    if value.startswith(_BASE64_PREFIX) and value.endswith(_BASE64_SUFFIX):
        encoded = value[len(_BASE64_PREFIX) : -len(_BASE64_SUFFIX)]
        try:
            decoded = base64.b64decode(encoded, validate=True).decode("utf-8")
        except (binascii.Error, UnicodeDecodeError) as exc:
            raise _header_mismatch(f"{name} header contains invalid Base64 encoding", request_id) from exc
        return decoded

    _validate_ascii_header(value, name, request_id)
    return value


def _header_mismatch(
    message: str,
    request_id: str | int | None,
) -> ProtocolRequestError:
    return ProtocolRequestError(
        HEADER_MISMATCH,
        f"Header mismatch: {message}",
        request_id=request_id,
    )
