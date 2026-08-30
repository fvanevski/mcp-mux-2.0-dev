from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

import httpx

MODERN_PROTOCOL_VERSION = "2026-07-28"
PROTOCOL_VERSION_META = "io.modelcontextprotocol/protocolVersion"
CLIENT_INFO_META = "io.modelcontextprotocol/clientInfo"
CLIENT_CAPABILITIES_META = "io.modelcontextprotocol/clientCapabilities"
SERVER_INFO_META = "io.modelcontextprotocol/serverInfo"

MockMode = Literal[
    "modern-stateless",
    "legacy-sessionful",
    "legacy-http-sse",
    "malformed-json",
    "http-failure",
    "transport-failure",
]


@dataclass(frozen=True)
class RecordedRequest:
    method: str
    path: str
    query: str
    headers: dict[str, str]
    json_body: object | None


@dataclass
class MockMCPUpstream:
    """Deterministic in-process MCP upstream for compatibility and failure-path tests."""

    mode: MockMode
    requests: list[RecordedRequest] = field(default_factory=list)
    legacy_session_id: str = "mock-session-001"

    @property
    def transport(self) -> httpx.MockTransport:
        return httpx.MockTransport(self._handle)

    def _record(self, request: httpx.Request) -> tuple[dict[str, str], object | None]:
        headers = {key.lower(): value for key, value in request.headers.items()}
        json_body: object | None = None
        if request.content:
            try:
                json_body = json.loads(request.content)
            except json.JSONDecodeError:
                json_body = None

        query = request.url.query
        if isinstance(query, bytes):
            query_text = query.decode("utf-8")
        else:
            query_text = str(query)

        self.requests.append(
            RecordedRequest(
                method=request.method,
                path=request.url.path,
                query=query_text,
                headers=headers,
                json_body=json_body,
            )
        )
        return headers, json_body

    @staticmethod
    def _json_response(
        payload: object,
        *,
        status_code: int = 200,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        response_headers = {"content-type": "application/json"}
        if headers:
            response_headers.update(headers)
        return httpx.Response(
            status_code,
            headers=response_headers,
            content=json.dumps(payload, separators=(",", ":")).encode("utf-8"),
        )

    @classmethod
    def _jsonrpc_error(
        cls,
        request_id: object,
        *,
        code: int,
        message: str,
        status_code: int = 400,
        data: object | None = None,
    ) -> httpx.Response:
        error: dict[str, object] = {"code": code, "message": message}
        if data is not None:
            error["data"] = data
        return cls._json_response(
            {"jsonrpc": "2.0", "id": request_id, "error": error},
            status_code=status_code,
        )

    def _handle(self, request: httpx.Request) -> httpx.Response:
        headers, json_body = self._record(request)

        if self.mode == "modern-stateless":
            return self._handle_modern(request, headers, json_body)
        if self.mode == "legacy-sessionful":
            return self._handle_legacy_sessionful(request, headers, json_body)
        if self.mode == "legacy-http-sse":
            return self._handle_legacy_http_sse(request)
        if self.mode == "malformed-json":
            return httpx.Response(
                200,
                headers={"content-type": "application/json"},
                content=b'{"jsonrpc":"2.0","id":1,"result":',
            )
        if self.mode == "http-failure":
            request_id = json_body.get("id") if isinstance(json_body, dict) else None
            return self._jsonrpc_error(
                request_id,
                code=-32000,
                message="deterministic upstream failure",
                status_code=503,
            )
        if self.mode == "transport-failure":
            raise httpx.ConnectError(
                "deterministic upstream transport failure",
                request=request,
            )
        raise AssertionError(f"Unsupported mock mode: {self.mode}")

    def _handle_modern(
        self,
        request: httpx.Request,
        headers: dict[str, str],
        json_body: object | None,
    ) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/mcp":
            return self._jsonrpc_error(
                None,
                code=-32600,
                message="modern endpoint is POST /mcp",
                status_code=405,
            )
        if not isinstance(json_body, dict):
            return self._jsonrpc_error(None, code=-32600, message="JSON-RPC object required")

        request_id = json_body.get("id")
        method = json_body.get("method")
        if json_body.get("jsonrpc") != "2.0" or not isinstance(request_id, (str, int)):
            return self._jsonrpc_error(request_id, code=-32600, message="valid JSON-RPC request required")
        if not isinstance(method, str):
            return self._jsonrpc_error(request_id, code=-32600, message="method is required")

        params = json_body.get("params")
        if not isinstance(params, dict):
            return self._jsonrpc_error(request_id, code=-32602, message="params object required")
        meta = params.get("_meta")
        if not isinstance(meta, dict):
            return self._jsonrpc_error(request_id, code=-32602, message="per-request _meta is required")

        body_version = meta.get(PROTOCOL_VERSION_META)
        if not isinstance(body_version, str):
            return self._jsonrpc_error(
                request_id,
                code=-32602,
                message=f"missing required {PROTOCOL_VERSION_META}",
            )
        client_capabilities = meta.get(CLIENT_CAPABILITIES_META)
        if not isinstance(client_capabilities, dict):
            return self._jsonrpc_error(
                request_id,
                code=-32602,
                message=f"missing required {CLIENT_CAPABILITIES_META}",
            )
        if headers.get("mcp-protocol-version") != body_version:
            return self._jsonrpc_error(
                request_id,
                code=-32020,
                message="MCP-Protocol-Version header mismatch",
            )
        if body_version != MODERN_PROTOCOL_VERSION:
            return self._jsonrpc_error(
                request_id,
                code=-32022,
                message="unsupported protocol version",
                data={"supportedVersions": [MODERN_PROTOCOL_VERSION]},
            )
        if headers.get("mcp-method") != method:
            return self._jsonrpc_error(request_id, code=-32020, message="Mcp-Method header mismatch")

        name_source: object | None = None
        if method in {"tools/call", "prompts/get"}:
            name_source = params.get("name")
        elif method == "resources/read":
            name_source = params.get("uri")
        if name_source is not None and headers.get("mcp-name") != name_source:
            return self._jsonrpc_error(request_id, code=-32020, message="Mcp-Name header mismatch")
        if method in {"tools/call", "prompts/get", "resources/read"} and not isinstance(name_source, str):
            return self._jsonrpc_error(request_id, code=-32602, message="named operation source is required")

        if method == "server/discover":
            result = {
                "resultType": "complete",
                "supportedVersions": [MODERN_PROTOCOL_VERSION],
                "capabilities": {"tools": {}},
                "_meta": {
                    SERVER_INFO_META: {
                        "name": "phase0-modern-mock",
                        "version": "0",
                    }
                },
                "ttlMs": 300000,
                "cacheScope": "public",
            }
        elif method == "tools/list":
            result = {
                "resultType": "complete",
                "tools": [
                    {
                        "name": "baseline_tool",
                        "description": "deterministic fixture",
                        "inputSchema": {
                            "type": "object",
                            "properties": {},
                            "additionalProperties": True,
                        },
                    }
                ],
                "ttlMs": 300000,
                "cacheScope": "public",
            }
        elif method == "tools/call":
            result = {
                "resultType": "complete",
                "content": [{"type": "text", "text": "baseline-ok"}],
                "isError": False,
            }
        elif method == "resources/read":
            result = {
                "resultType": "complete",
                "contents": [{"uri": name_source, "text": "baseline-resource"}],
            }
        else:
            result = {"resultType": "complete", "echoMethod": method}

        return self._json_response({"jsonrpc": "2.0", "id": request_id, "result": result})

    def _handle_legacy_sessionful(
        self,
        request: httpx.Request,
        headers: dict[str, str],
        json_body: object | None,
    ) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/mcp":
            return self._json_response({"error": "legacy endpoint is POST /mcp"}, status_code=405)
        if not isinstance(json_body, dict):
            return self._json_response({"error": "JSON-RPC object required"}, status_code=400)

        method = json_body.get("method")
        if method == "initialize":
            return self._json_response(
                {
                    "jsonrpc": "2.0",
                    "id": json_body.get("id"),
                    "result": {
                        "protocolVersion": "2025-11-25",
                        "capabilities": {"tools": {}},
                        "serverInfo": {"name": "phase0-mock", "version": "0"},
                    },
                },
                headers={"mcp-session-id": self.legacy_session_id},
            )

        if headers.get("mcp-session-id") != self.legacy_session_id:
            return self._json_response({"error": "legacy session id required"}, status_code=400)

        if method == "tools/list":
            result = {
                "tools": [
                    {
                        "name": "legacy_tool",
                        "description": "deterministic fixture",
                        "inputSchema": {"type": "object", "properties": {}},
                    }
                ]
            }
        else:
            result = {"echoMethod": method}
        return self._json_response(
            {"jsonrpc": "2.0", "id": json_body.get("id"), "result": result}
        )

    def _handle_legacy_http_sse(self, request: httpx.Request) -> httpx.Response:
        if request.method == "GET" and request.url.path == "/mcp/sse":
            return httpx.Response(
                200,
                headers={"content-type": "text/event-stream"},
                content=(
                    "event: endpoint\n"
                    "data: /mcp/messages/?session_id=legacy-upstream-001\n\n"
                ),
            )

        if request.method == "POST" and request.url.path == "/mcp/messages/":
            if request.url.params.get("session_id") != "legacy-upstream-001":
                return self._json_response({"error": "legacy SSE session required"}, status_code=400)
            return self._json_response({"accepted": True}, status_code=202)

        return self._json_response({"error": "legacy SSE route not found"}, status_code=404)
