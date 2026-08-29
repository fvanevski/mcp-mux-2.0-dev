from __future__ import annotations

import asyncio
import inspect
import logging
import os
import re
from collections.abc import Callable
from typing import Annotated, Any, Literal
from urllib.parse import urlsplit

import yaml
from pydantic import BaseModel, ConfigDict, Field, TypeAdapter, field_validator, model_validator

logger = logging.getLogger(__name__)
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")
ROUTE_NAME_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._-]*$")
ENV_NAME_PATTERN = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


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


def load_router_config(config_path: str) -> "RouterConfig":
    with open(config_path, encoding="utf-8") as config_file:
        data = yaml.safe_load(config_file) or {}
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


def _validate_optional_text(value: str | None, field_name: str) -> str | None:
    if value is None:
        return None
    candidate = value.strip()
    if not candidate:
        raise ValueError(f"{field_name} must not be empty")
    if "\x00" in candidate:
        raise ValueError(f"{field_name} must not contain NUL bytes")
    return candidate


def _validate_tool_list(value: list[str] | None) -> list[str] | None:
    if value is None:
        return None
    cleaned: list[str] = []
    seen: set[str] = set()
    for tool in value:
        candidate = tool.strip()
        if not candidate:
            raise ValueError("tool names must not be empty")
        if candidate in seen:
            raise ValueError(f"duplicate tool name: {candidate}")
        seen.add(candidate)
        cleaned.append(candidate)
    return cleaned


class EndpointBase(BaseModel):
    model_config = ConfigDict(extra="forbid")

    path: str
    url: str
    summary: str
    timeout: float = Field(default=300.0, gt=0)
    transport: Literal["sse", "streamable-http"] | None = None
    legacy_sse_bridge: bool = False
    allowed_tools: list[str] | None = None
    denied_tools: list[str] | None = None
    headers: dict[str, str] | None = None

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

    @field_validator("allowed_tools", "denied_tools")
    @classmethod
    def validate_tools(cls, value: list[str] | None) -> list[str] | None:
        return _validate_tool_list(value)

    @field_validator("headers")
    @classmethod
    def normalize_headers(cls, value: dict[str, str] | None) -> dict[str, str] | None:
        if not value:
            return None
        cleaned_headers: dict[str, str] = {}
        seen_names: set[str] = set()
        for key, raw_value in value.items():
            header_name = key.strip()
            if not header_name:
                raise ValueError("header names must not be empty")
            normalized_name = header_name.casefold()
            if normalized_name in seen_names:
                raise ValueError(f"duplicate header name: {header_name}")
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

    @model_validator(mode="after")
    def finalize_common_fields(self) -> "EndpointBase":
        if self.allowed_tools is not None and self.denied_tools is not None:
            self.denied_tools = None

        if self.transport is None:
            parsed = urlsplit(self.url)
            self.transport = (
                "streamable-http"
                if parsed.path.endswith("/mcp") or "/mcp/" in parsed.path
                else "sse"
            )

        if self.legacy_sse_bridge and self.transport != "streamable-http":
            raise ValueError("legacy_sse_bridge requires transport 'streamable-http'")
        return self


class RemoteEndpointConfig(EndpointBase):
    mode: Literal["remote"]


class ManagedReadinessConfig(BaseModel):
    model_config = ConfigDict(extra="forbid")

    host: str | None = None
    port: int | None = Field(default=None, ge=1, le=65535)
    timeout: float = Field(default=15.0, gt=0)
    interval: float = Field(default=0.2, gt=0)

    @field_validator("host")
    @classmethod
    def validate_host(cls, value: str | None) -> str | None:
        return _validate_optional_text(value, "readiness.host")


class ManagedEndpointConfig(EndpointBase):
    mode: Literal["managed_cli"]
    argv: list[str] | None = None
    env: dict[str, str] = Field(default_factory=dict)
    cwd: str | None = None
    readiness: ManagedReadinessConfig = Field(default_factory=ManagedReadinessConfig)
    unsafe_shell_command: str | None = None

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
    def finalize_managed_fields(self) -> "ManagedEndpointConfig":
        execution_sources = int(self.argv is not None) + int(self.unsafe_shell_command is not None)
        if execution_sources != 1:
            raise ValueError("managed_cli requires exactly one of argv or unsafe_shell_command")

        parsed = urlsplit(self.url)
        if self.readiness.host is None:
            self.readiness.host = parsed.hostname
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
    model_config = ConfigDict(extra="forbid")

    endpoints: list[Endpoint]

    @model_validator(mode="after")
    def validate_ports_and_paths(self) -> "RouterConfig":
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
        except Exception as exc:
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
            except Exception as exc:
                logger.error("Error in config watcher poll loop: %s", exc)
            await asyncio.sleep(self.poll_interval)
