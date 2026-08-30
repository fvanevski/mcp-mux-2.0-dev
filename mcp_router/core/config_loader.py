from __future__ import annotations

import asyncio
import inspect
import ipaddress
import logging
import os
import re
from collections.abc import Callable
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import (
    BaseModel,
    ConfigDict,
    Field,
    SecretStr,
    TypeAdapter,
    field_validator,
    model_validator,
)

logger = logging.getLogger(__name__)
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
ROUTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
HEADER_NAME_PATTERN = re.compile(r"^[!#$%&'*+\-.^_`|~0-9A-Za-z]+$")
_FORBIDDEN_INBOUND_HEADERS = {
    "authorization",
    "cookie",
    "proxy-authorization",
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
    "forwarded",
    "x-forwarded-for",
    "x-forwarded-host",
    "x-forwarded-proto",
    "x-forwarded-user",
    "x-real-ip",
}
_FORBIDDEN_UPSTREAM_INJECTED_HEADERS = {
    "host",
    "content-length",
    "connection",
    "transfer-encoding",
    "keep-alive",
    "te",
    "trailer",
    "upgrade",
}


def expand_env_vars(value: Any) -> Any:
    if isinstance(value, str):
        def replace(match: re.Match[str]) -> str:
            name = match.group(1)
            default = match.group(2)
            env_value = os.environ.get(name)
            if env_value is not None:
                return env_value
            if default is not None:
                return default
            raise ValueError(f"Environment variable '{name}' is required but not set")

        return ENV_VAR_PATTERN.sub(replace, value)
    if isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    if isinstance(value, dict):
        return {key: expand_env_vars(item) for key, item in value.items()}
    return value


def load_router_config(config_path: str) -> RouterConfig:
    try:
        with open(config_path, encoding="utf-8") as config_file:
            data = yaml.safe_load(config_file) or {}
    except yaml.YAMLError:
        raise ValueError("Router configuration contains invalid YAML") from None
    return RouterConfig.model_validate(expand_env_vars(data))


def _validate_http_url(value: str) -> str:
    candidate = value.strip()
    if not candidate:
        raise ValueError("url must not be empty")
    try:
        parsed = urlsplit(candidate)
        _ = parsed.port
    except ValueError as exc:
        raise ValueError(f"invalid endpoint url: {exc}") from exc
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("url must be an absolute http or https URL")
    return candidate


def _is_loopback_host(host: str) -> bool:
    normalized = host.rstrip(".").casefold()
    if normalized == "localhost" or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _validate_optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field_name} must not be empty")
    if "\x00" in candidate:
        raise ValueError(f"{field_name} must not contain NUL bytes")
    return candidate


def _validate_name_list(value: list[str] | None, label: str) -> list[str] | None:
    if value is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for item in value:
        candidate = item.strip()
        if not candidate:
            raise ValueError(f"{label} must not contain empty names")
        if candidate in seen:
            raise ValueError(f"duplicate {label} name: {candidate}")
        seen.add(candidate)
        cleaned.append(candidate)
    return cleaned


def _validate_header_name(value: str, field_name: str) -> str:
    candidate = value.strip()
    if not candidate or not HEADER_NAME_PATTERN.fullmatch(candidate):
        raise ValueError(f"{field_name} contains an invalid HTTP header name")
    return candidate


class SecurityConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    mode: Literal["local_only", "authenticated"] = "local_only"
    allowed_hosts: list[str] = Field(
        default_factory=lambda: ["127.0.0.1", "localhost", "::1"],
    )
    allowed_origins: list[str] = Field(default_factory=list)
    api_key: SecretStr | None = None
    trusted_proxies: list[str] = Field(default_factory=list)
    trusted_proxy_identity_header: str = "X-Forwarded-User"

    @field_validator("allowed_hosts")
    @classmethod
    def validate_allowed_hosts(cls, value: list[str]) -> list[str]:
        if not value:
            raise ValueError("security.allowed_hosts must contain at least one host")
        cleaned: list[str] = []
        seen: set[str] = set()
        for host in value:
            candidate = host.strip().casefold()
            if (
                not candidate
                or candidate == "*"
                or "/" in candidate
                or any(char.isspace() for char in candidate)
            ):
                raise ValueError("security.allowed_hosts must contain explicit host names or IP addresses")
            if candidate not in seen:
                seen.add(candidate)
                cleaned.append(candidate)
        return cleaned

    @field_validator("allowed_origins")
    @classmethod
    def validate_allowed_origins(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for origin in value:
            candidate = origin.strip()
            if not candidate or candidate == "*":
                raise ValueError("security.allowed_origins must contain explicit origins")
            parsed = urlsplit(candidate)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.netloc
                or parsed.path not in {"", "/"}
                or parsed.query
                or parsed.fragment
            ):
                raise ValueError("security.allowed_origins entries must be absolute HTTP origins")
            normalized = f"{parsed.scheme.casefold()}://{parsed.netloc.casefold()}"
            if normalized not in seen:
                seen.add(normalized)
                cleaned.append(normalized)
        return cleaned

    @field_validator("api_key")
    @classmethod
    def validate_api_key(cls, value: SecretStr | None) -> SecretStr | None:
        if value is None:
            return None
        secret = value.get_secret_value().strip()
        if not secret:
            raise ValueError("security.api_key must not be empty")
        return SecretStr(secret)

    @field_validator("trusted_proxies")
    @classmethod
    def validate_trusted_proxies(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for network in value:
            candidate = network.strip()
            try:
                normalized = str(ipaddress.ip_network(candidate, strict=False))
            except ValueError as exc:
                raise ValueError(f"invalid trusted proxy network: {candidate}") from exc
            if normalized not in seen:
                seen.add(normalized)
                cleaned.append(normalized)
        return cleaned

    @field_validator("trusted_proxy_identity_header")
    @classmethod
    def validate_identity_header(cls, value: str) -> str:
        return _validate_header_name(value, "security.trusted_proxy_identity_header")

    @model_validator(mode="after")
    def validate_authentication_provider(self) -> SecurityConfig:
        if self.mode == "authenticated" and self.api_key is None and not self.trusted_proxies:
            raise ValueError(
                "security.mode='authenticated' requires api_key and/or trusted_proxies"
            )
        return self


class RequestLimitConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    max_concurrent: int | None = Field(default=None, ge=1)
    requests_per_minute: int | None = Field(default=None, ge=1)


class LegacySSEBridgeConfig(BaseModel):
    """Explicit bounded policy for the deprecated local SSE compatibility adapter."""

    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    queue_capacity: int = Field(default=32, ge=1, le=4096)
    backpressure_timeout: float = Field(default=1.0, gt=0, le=60.0)
    session_ttl: float = Field(default=300.0, gt=0, le=86400.0)
    max_sessions: int = Field(default=32, ge=1, le=4096)


class EndpointBase(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    path: str
    url: str
    summary: str
    timeout: float = Field(default=300.0, gt=0)
    upstream_timeout: float = Field(default=60.0, gt=0)
    transport: Literal["sse", "streamable-http"] | None = None
    legacy_sse_bridge: LegacySSEBridgeConfig | None = None
    allowed_methods: list[str] | None = None
    denied_methods: list[str] | None = None
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    allowed_resources: list[str] | None = None
    denied_resources: list[str] | None = None
    allowed_prompts: list[str] | None = None
    denied_prompts: list[str] | None = None
    inbound_headers: list[str] = Field(default_factory=list)
    headers: dict[str, str] | None = None
    limits: RequestLimitConfig = Field(default_factory=RequestLimitConfig)
    tool_limits: dict[str, RequestLimitConfig] = Field(default_factory=dict)

    @field_validator("path")
    @classmethod
    def validate_path(cls, value: str) -> str:
        path = value.strip()
        if not path:
            raise ValueError("path must not be empty")
        if path.casefold() == "summary":
            raise ValueError("path 'summary' is reserved")
        if "/" in path:
            raise ValueError("path must be a single route namespace and cannot contain '/'")
        if not ROUTE_NAME_PATTERN.fullmatch(path):
            raise ValueError("path contains unsupported characters")
        return path

    @field_validator("url")
    @classmethod
    def validate_url(cls, value: str) -> str:
        return _validate_http_url(value)

    @field_validator("summary")
    @classmethod
    def validate_summary(cls, value: str) -> str:
        summary = value.strip()
        if not summary:
            raise ValueError("summary must not be empty")
        return summary

    @field_validator(
        "allowed_methods",
        "denied_methods",
        "allowed_tools",
        "denied_tools",
        "allowed_resources",
        "denied_resources",
        "allowed_prompts",
        "denied_prompts",
    )
    @classmethod
    def validate_policy_names(cls, value: list[str] | None) -> list[str] | None:
        return _validate_name_list(value, "policy")

    @field_validator("inbound_headers")
    @classmethod
    def normalize_inbound_headers(cls, value: list[str]) -> list[str]:
        cleaned: list[str] = []
        seen: set[str] = set()
        for raw_name in value:
            name = _validate_header_name(raw_name, "inbound_headers")
            normalized = name.casefold()
            if normalized in _FORBIDDEN_INBOUND_HEADERS or normalized.startswith("x-forwarded-"):
                raise ValueError(
                    f"inbound header '{name}' is reserved for gateway security or HTTP transport"
                )
            if normalized not in seen:
                seen.add(normalized)
                cleaned.append(name)
        return cleaned

    @field_validator("headers")
    @classmethod
    def normalize_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if not value:
            return None
        cleaned_headers: dict[str, str] = {}
        seen_names: set[str] = set()
        for key, raw_value in value.items():
            header_name = _validate_header_name(key, "headers")
            normalized_name = header_name.casefold()
            if normalized_name in seen_names:
                raise ValueError(f"duplicate header name: {header_name}")
            if normalized_name in _FORBIDDEN_UPSTREAM_INJECTED_HEADERS:
                raise ValueError(f"upstream header '{header_name}' is controlled by HTTP transport")
            seen_names.add(normalized_name)

            normalized_value = raw_value.strip()
            if normalized_name == "authorization":
                lower_value = normalized_value.casefold()
                if lower_value == "bearer":
                    continue
                if lower_value.startswith("bearer bearer "):
                    normalized_value = "Bearer " + normalized_value.split(None, 2)[2]
            if normalized_value:
                cleaned_headers[header_name] = normalized_value
        return cleaned_headers or None

    @field_validator("tool_limits")
    @classmethod
    def validate_tool_limits(
        cls,
        value: dict[str, RequestLimitConfig],
    ) -> dict[str, RequestLimitConfig]:
        cleaned: dict[str, RequestLimitConfig] = {}
        for raw_name, limits in value.items():
            name = raw_name.strip()
            if not name:
                raise ValueError("tool_limits keys must not be empty")
            if name in cleaned:
                raise ValueError(f"duplicate tool limit: {name}")
            cleaned[name] = limits
        return cleaned

    @model_validator(mode="after")
    def finalize_common_fields(self) -> EndpointBase:
        for allow_name, deny_name in (
            ("allowed_methods", "denied_methods"),
            ("allowed_tools", "denied_tools"),
            ("allowed_resources", "denied_resources"),
            ("allowed_prompts", "denied_prompts"),
        ):
            if getattr(self, allow_name) is not None and getattr(self, deny_name) is not None:
                raise ValueError(f"{allow_name} and {deny_name} are mutually exclusive")

        if self.transport is None:
            parsed = urlsplit(self.url)
            self.transport = (
                "streamable-http"
                if parsed.path.endswith("/mcp") or "/mcp/" in parsed.path
                else "sse"
            )

        if self.legacy_sse_bridge is not None and self.transport != "streamable-http":
            raise ValueError("legacy_sse_bridge requires transport 'streamable-http'")
        return self


class RemoteEndpointConfig(EndpointBase):
    mode: Literal["remote"]


class ManagedRestartConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    enabled: bool = False
    max_attempts: int = Field(default=3, ge=1, le=100)
    initial_backoff: float = Field(default=0.5, gt=0)
    max_backoff: float = Field(default=10.0, gt=0)

    @model_validator(mode="after")
    def validate_backoff_bounds(self) -> ManagedRestartConfig:
        if self.max_backoff < self.initial_backoff:
            raise ValueError("restart.max_backoff must be greater than or equal to initial_backoff")
        return self


class ManagedReadinessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    timeout: float = Field(default=15.0, gt=0)
    interval: float = Field(default=0.2, gt=0)
    legacy_initialize_fallback: bool = False

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        return _validate_optional_text(value, "readiness.host")


class ManagedEndpointConfig(EndpointBase):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    mode: Literal["managed_cli"]
    argv: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    readiness: ManagedReadinessConfig = Field(default_factory=ManagedReadinessConfig)
    restart: ManagedRestartConfig = Field(default_factory=ManagedRestartConfig)
    unsafe_shell_command: str | None = None
    allow_non_loopback_target: bool = False

    @field_validator("argv")
    @classmethod
    def validate_argv(cls, value: list[str] | None) -> list[str] | None:
        if value is None:
            return None
        if not value:
            raise ValueError("argv must contain at least one element")
        cleaned: list[str] = []
        for argument in value:
            if "\x00" in argument:
                raise ValueError("argv entries must not contain NUL bytes")
            if not argument.strip():
                raise ValueError("argv entries must not be empty")
            cleaned.append(argument)
        return cleaned

    @field_validator("env")
    @classmethod
    def validate_env(cls, value: dict[str, str]) -> dict[str, str]:
        for name, env_value in value.items():
            if not ENV_NAME_PATTERN.fullmatch(name):
                raise ValueError(f"invalid environment variable name: {name}")
            if "\x00" in env_value:
                raise ValueError(f"environment variable '{name}' contains a NUL byte")
        return value

    @field_validator("cwd")
    @classmethod
    def validate_cwd(cls, value: str | None) -> str | None:
        return _validate_optional_text(value, "cwd")

    @field_validator("unsafe_shell_command")
    @classmethod
    def validate_unsafe_shell_command(cls, value: str | None) -> str | None:
        return _validate_optional_text(value, "unsafe_shell_command")

    @model_validator(mode="after")
    def finalize_managed_fields(self) -> ManagedEndpointConfig:
        execution_sources = int(self.argv is not None) + int(self.unsafe_shell_command is not None)
        if execution_sources != 1:
            raise ValueError("managed_cli requires exactly one of argv or unsafe_shell_command")

        parsed = urlsplit(self.url)
        target_host = parsed.hostname
        if target_host is None:
            raise ValueError("managed endpoint url must include a target host")
        if not self.allow_non_loopback_target and not _is_loopback_host(target_host):
            raise ValueError(
                "managed endpoint url must target loopback unless allow_non_loopback_target is true"
            )
        if self.readiness.host is None:
            self.readiness.host = target_host
        if self.readiness.port is None:
            self.readiness.port = parsed.port or (443 if parsed.scheme == "https" else 80)
        return self


Endpoint = Annotated[
    RemoteEndpointConfig | ManagedEndpointConfig,
    Field(discriminator="mode"),
]
_ENDPOINT_ADAPTER = TypeAdapter(Endpoint)


def EndpointConfig(**data: Any) -> Endpoint:
    """Compatibility constructor for callers that instantiate endpoint configs directly."""
    return _ENDPOINT_ADAPTER.validate_python(data)


class RouterConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", hide_input_in_errors=True)

    endpoints: list[Endpoint]
    max_request_body_bytes: int = Field(default=1_048_576, ge=1024, le=67_108_864)
    security: SecurityConfig = Field(default_factory=SecurityConfig)

    @model_validator(mode="after")
    def validate_ports_and_paths(self) -> RouterConfig:
        ports: set[int] = set()
        paths: set[str] = set()
        for endpoint in self.endpoints:
            if endpoint.path in paths:
                raise ValueError(f"Duplicate path detected: {endpoint.path}")
            paths.add(endpoint.path)

            if isinstance(endpoint, ManagedEndpointConfig):
                port = endpoint.readiness.port
                if port is None:
                    raise ValueError(f"managed endpoint '{endpoint.path}' has no readiness port")
                if port in ports:
                    raise ValueError(f"Duplicate port detected: {port}")
                ports.add(port)
        return self


class ConfigWatcher:
    """Poll a config file and invoke the callback only after successful validation."""

    def __init__(
        self,
        config_path: str,
        callback: Callable[[RouterConfig], object],
        poll_interval: float = 1.0,
    ):
        self.config_path = config_path
        self.callback = callback
        self.poll_interval = poll_interval
        self._last_mtime: float | None = None
        self._task: asyncio.Task[None] | None = None
        self._running = False

    async def start(self) -> None:
        if os.path.exists(self.config_path):
            initial_config = load_router_config(self.config_path)
            result = self.callback(initial_config)
            if inspect.isawaitable(result):
                await result
            self._last_mtime = os.path.getmtime(self.config_path)

        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info("Started config polling for: %s", self.config_path)

    async def stop(self) -> None:
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped config polling.")

    async def _reload_if_changed(self) -> bool:
        if not os.path.exists(self.config_path):
            return False
        mtime = os.path.getmtime(self.config_path)
        if self._last_mtime is not None and mtime == self._last_mtime:
            return False
        self._last_mtime = mtime
        logger.info("Detected change in configuration: %s", self.config_path)
        try:
            new_config = load_router_config(self.config_path)
        except (OSError, ValueError, yaml.YAMLError) as exc:
            logger.error("Failed to load or validate new config: %s", exc)
            return False

        result = self.callback(new_config)
        if inspect.isawaitable(result):
            await result
        return True

    async def _poll_loop(self) -> None:
        while self._running:
            try:
                await self._reload_if_changed()
            except (OSError, RuntimeError, ValueError) as exc:
                logger.error("Error in config watcher poll loop: %s", exc)
            await asyncio.sleep(self.poll_interval)
