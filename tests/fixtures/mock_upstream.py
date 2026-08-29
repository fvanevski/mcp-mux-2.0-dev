from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Literal

import httpx

MockMode = Literal[
    "modern-stateless",
    "legacy-sessionful",
    "legacy-http-sse",
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
    """Deterministic in-process MCP upstream used by Phase 0 compatibility tests."""

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

    def _handle(self, request: httpx.Request) -> httpx.Response:
        headers, json_body = self._record(request)

        if self.mode == "modern-stateless":
            return self._handle_modern(request, headers, json_body)
        if self.mode == "legacy-sessionful":
            return self._handle_legacy_sessionful(request, headers, json_body)
        if self.mode == "legacy-http-sse":
            return self._handle_legacy_http_sse(request)
        raise AssertionError(f"Unsupported mock mode: {self.mode}")

    def _handle_modern(
        self,
        request: httpx.Request,
        headers: dict[str, str],
        json_body: object | None,
    ) -> httpx.Response:
        if request.method != "POST" or request.url.path != "/mcp":
            return self._json_response({"error": "modern endpoint is POST /mcp"}, status_code=405)
        if not isinstance(json_body, dict):
            return self._json_response({"error": "JSON-RPC object required"}, status_code=400)
        if headers.get("mcp-protocol-version") != "2026-07-28":
            return self._json_response({"error": "missing protocol version"}, status_code=400)
        if headers.get("mcp-method") != json_body.get("method"):
            return self._json_response({"error": "method header mismatch"}, status_code=400)
        if "mcp-session-id" in headers:
            return self._json_response({"error": "modern requests are stateless"}, status_code=400)

        method = json_body.get("method")
        params = json_body.get("params") if isinstance(json_body.get("params"), dict) else {}
        if method in {"tools/call", "resources/read", "prompts/get"}:
            if headers.get("mcp-name") != params.get("name"):
                return self._json_response({"error": "name header mismatch"}, status_code=400)

        if method == "server/discover":
            result = {"protocolVersion": "2026-07-28", "capabilities": {"tools": {}}}
        elif method == "tools/list":
            result = {"tools": [{"name": "baseline_tool", "description": "deterministic fixture"}]}
        elif method == "tools/call":
            result = {"content": [{"type": "text", "text": "baseline-ok"}]}
        else:
            result = {"echoMethod": method}

        return self._json_response(
            {"jsonrpc": "2.0", "id": json_body.get("id"), "result": result}
        )

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
            result = {"tools": [{"name": "legacy_tool", "description": "deterministic fixture"}]}
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
                ).encode("utf-8"),
            )

        if request.method == "POST" and request.url.path == "/mcp/messages/":
            if request.url.params.get("session_id") != "legacy-upstream-001":
                return self._json_response({"error": "legacy SSE session required"}, status_code=400)
            return self._json_response({"accepted": True}, status_code=202)

        return self._json_response({"error": "legacy SSE route not found"}, status_code=404)
