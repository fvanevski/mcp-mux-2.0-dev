from __future__ import annotations

import hmac
import ipaddress
from collections.abc import Awaitable, Callable, Mapping, MutableMapping
from typing import Any

from starlette.responses import JSONResponse

from mcp_router.core.config_loader import Endpoint, SecurityConfig

ASGIMessage = MutableMapping[str, Any]
ASGIReceive = Callable[[], Awaitable[ASGIMessage]]
ASGISend = Callable[[ASGIMessage], Awaitable[None]]
ASGIApp = Callable[[MutableMapping[str, Any], ASGIReceive, ASGISend], Awaitable[None]]

_HOP_BY_HOP_REQUEST_HEADERS = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
}
_CALLER_CREDENTIAL_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
}
_FORWARDED_IDENTITY_HEADERS = {
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-user",
    "x-real-ip",
}
_DEFAULT_UPSTREAM_REQUEST_HEADERS = {
    "accept",
    "content-type",
    "mcp-protocol-version",
    "mcp-method",
    "mcp-name",
    "mcp-session-id",
    "last-event-id",
    "traceparent",
    "tracestate",
    "baggage",
}
_SENSITIVE_RESPONSE_HEADERS = {
    "authorization",
    "proxy-authorization",
    "set-cookie",
}


def is_loopback_host(host: str) -> bool:
    candidate = host.strip().casefold()
    if candidate == "localhost":
        return True
    try:
        return ipaddress.ip_address(candidate).is_loopback
    except ValueError:
        return False


def validate_bind_security(host: str, security: SecurityConfig) -> None:
    if is_loopback_host(host):
        return
    if security.mode != "authenticated":
        raise ValueError(
            "Non-loopback binding requires security.mode='authenticated' with an explicit authentication provider"
        )


def build_upstream_headers(
    inbound: Mapping[str, str],
    endpoint: Endpoint,
) -> dict[str, str]:
    extra = {name.casefold() for name in endpoint.inbound_headers}
    headers: dict[str, str] = {}
    for key, value in inbound.items():
        lower = key.casefold()
        if (
            lower in _HOP_BY_HOP_REQUEST_HEADERS
            or lower in _CALLER_CREDENTIAL_HEADERS
            or lower in _FORWARDED_IDENTITY_HEADERS
            or lower.startswith("x-forwarded-")
        ):
            continue
        if (
            lower in _DEFAULT_UPSTREAM_REQUEST_HEADERS
            or lower.startswith("mcp-param-")
            or lower in extra
        ):
            headers[key] = value

    if endpoint.headers:
        headers.update(endpoint.headers)
    return headers


def sanitize_response_headers(
    headers: Mapping[str, str],
    *,
    body_was_decoded: bool = False,
    body_was_transformed: bool = False,
) -> dict[str, str]:
    skipped = set(_HOP_BY_HOP_REQUEST_HEADERS)
    skipped.update(_SENSITIVE_RESPONSE_HEADERS)
    if body_was_decoded:
        skipped.update({"content-encoding", "content-length"})
    if body_was_transformed:
        skipped.update(
            {
                "content-encoding",
                "content-length",
                "etag",
                "digest",
                "content-digest",
                "repr-digest",
                "content-md5",
                "last-modified",
                "expires",
                "age",
            }
        )
    return {key: value for key, value in headers.items() if key.casefold() not in skipped}


def _scope_headers(scope: Mapping[str, Any]) -> dict[str, str]:
    return {
        key.decode("latin-1").casefold(): value.decode("latin-1")
        for key, value in scope.get("headers", [])
    }


def _host_name(value: str) -> str:
    candidate = value.strip()
    if candidate.startswith("["):
        closing = candidate.find("]")
        if closing > 0:
            return candidate[1:closing].casefold()
    host, separator, port = candidate.rpartition(":")
    if separator and host and port.isdigit():
        return host.casefold()
    return candidate.casefold()


def _client_ip(scope: Mapping[str, Any]) -> str | None:
    client = scope.get("client")
    if not isinstance(client, (tuple, list)) or not client:
        return None
    value = client[0]
    return value if isinstance(value, str) else None


def _peer_is_trusted(peer: str | None, security: SecurityConfig) -> bool:
    if peer is None:
        return False
    try:
        address = ipaddress.ip_address(peer)
    except ValueError:
        return False
    return any(address in ipaddress.ip_network(network, strict=False) for network in security.trusted_proxies)


def _bearer_token(value: str | None) -> str | None:
    if value is None:
        return None
    scheme, separator, token = value.partition(" ")
    if not separator or scheme.casefold() != "bearer":
        return None
    token = token.strip()
    return token or None


class GatewaySecurityMiddleware:
    """Authenticate the caller and validate HTTP exposure before endpoint routing."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        get_config: Callable[[], SecurityConfig],
    ) -> None:
        self.app = app
        self.get_config = get_config

    async def __call__(
        self,
        scope: MutableMapping[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
    ) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        security = self.get_config()
        headers = _scope_headers(scope)
        host = _host_name(headers.get("host", ""))
        if not host or host not in security.allowed_hosts:
            await self._reject(scope, receive, send, 403, "Invalid Host")
            return

        peer = _client_ip(scope)
        if security.mode == "local_only" and (peer is None or not is_loopback_host(peer)):
            await self._reject(scope, receive, send, 403, "Local-only gateway access required")
            return

        origin = headers.get("origin")
        if origin is not None and origin not in security.allowed_origins:
            await self._reject(scope, receive, send, 403, "Invalid Origin")
            return

        method = str(scope.get("method", "")).upper()
        if (
            method == "OPTIONS"
            and origin is not None
            and headers.get("access-control-request-method")
        ):
            response_headers = self._cors_headers(origin)
            requested_headers = headers.get("access-control-request-headers")
            if requested_headers:
                response_headers["Access-Control-Allow-Headers"] = requested_headers
            response_headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, DELETE, OPTIONS"
            response = JSONResponse({}, status_code=204, headers=response_headers)
            await response(scope, receive, send)
            return

        principal: str
        if security.mode == "local_only":
            principal = "local"
        else:
            authenticated_principal = self._authenticated_principal(headers, peer, security)
            if authenticated_principal is None:
                await self._reject(
                    scope,
                    receive,
                    send,
                    401,
                    "Caller authentication required",
                    extra_headers={"WWW-Authenticate": "Bearer"},
                )
                return
            principal = authenticated_principal

        scope["mcp.principal"] = principal

        if origin is None:
            await self.app(scope, receive, send)
            return

        async def cors_send(message: ASGIMessage) -> None:
            if message.get("type") == "http.response.start":
                raw_headers = list(message.get("headers", []))
                for key, value in self._cors_headers(origin).items():
                    raw_headers.append((key.lower().encode("latin-1"), value.encode("latin-1")))
                message["headers"] = raw_headers
            await send(message)

        await self.app(scope, receive, cors_send)

    def _authenticated_principal(
        self,
        headers: Mapping[str, str],
        peer: str | None,
        security: SecurityConfig,
    ) -> str | None:
        if security.trusted_proxies and _peer_is_trusted(peer, security):
            identity = headers.get(security.trusted_proxy_identity_header.casefold())
            if identity is not None:
                identity = identity.strip()
                if identity:
                    return f"proxy:{identity}"

        configured_key = security.api_key
        if configured_key is None:
            return None
        supplied = _bearer_token(headers.get("authorization"))
        if supplied is None:
            return None
        if hmac.compare_digest(supplied, configured_key.get_secret_value()):
            return "api-key"
        return None

    @staticmethod
    def _cors_headers(origin: str) -> dict[str, str]:
        return {
            "Access-Control-Allow-Origin": origin,
            "Access-Control-Allow-Credentials": "true",
            "Vary": "Origin",
        }

    @staticmethod
    async def _reject(
        scope: MutableMapping[str, Any],
        receive: ASGIReceive,
        send: ASGISend,
        status_code: int,
        message: str,
        *,
        extra_headers: dict[str, str] | None = None,
    ) -> None:
        response = JSONResponse(
            {"error": message},
            status_code=status_code,
            headers=extra_headers,
        )
        await response(scope, receive, send)
