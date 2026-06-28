import asyncio
import os
import logging
import re
from typing import List, Optional, Callable
import yaml
from pydantic import BaseModel, Field, model_validator

logger = logging.getLogger(__name__)
ENV_VAR_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)(?::-([^}]*))?\}")

def expand_env_vars(value):
    if isinstance(value, str):
        def replace(match: re.Match) -> str:
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
    with open(config_path, "r") as f:
        data = yaml.safe_load(f) or {}
    return RouterConfig.model_validate(expand_env_vars(data))

class EndpointConfig(BaseModel):
    path: str
    mode: str  # "remote", "managed_cli", "stdio_bridge"
    url: Optional[str] = None
    command: Optional[str] = None
    args: List[str] = Field(default_factory=list)
    port: Optional[int] = None
    summary: str
    timeout: int = 300  # Default to 300 seconds (5 minutes)
    transport: Optional[str] = None
    allowed_tools: Optional[List[str]] = None
    denied_tools: Optional[List[str]] = None
    headers: Optional[dict[str, str]] = None

    @model_validator(mode="after")
    def validate_mode_requirements(self) -> "EndpointConfig":
        """Validate configuration requirements based on the mode."""
        if self.headers:
            cleaned_headers = {}
            for key, value in self.headers.items():
                normalized_value = value.strip()
                if key.lower() == "authorization":
                    lower_value = normalized_value.lower()
                    if lower_value == "bearer":
                        continue
                    if lower_value.startswith("bearer bearer "):
                        normalized_value = "Bearer " + normalized_value.split(None, 2)[2]
                if normalized_value:
                    cleaned_headers[key] = normalized_value
            self.headers = cleaned_headers or None

        if self.allowed_tools is not None and self.denied_tools is not None:
            self.denied_tools = None

        if self.transport is None:
            if self.url and (self.url.endswith("/mcp") or "/mcp/" in self.url):
                self.transport = "streamable-http"
            else:
                self.transport = "sse"

        if self.mode == "remote":
            if not self.url:
                raise ValueError("url is required for remote mode")
        elif self.mode == "managed_cli":
            if not self.command:
                raise ValueError("command is required for managed_cli mode")
            if not self.url:
                raise ValueError("url is required for managed_cli mode")
        elif self.mode == "stdio_bridge":
            if not self.command:
                raise ValueError("command is required for stdio_bridge mode")
            if not self.port:
                raise ValueError("port is required for stdio_bridge mode")
        else:
            raise ValueError(f"Invalid mode: {self.mode}")
        return self

class RouterConfig(BaseModel):
    endpoints: List[EndpointConfig]

    @model_validator(mode="after")
    def validate_ports_and_paths(self) -> "RouterConfig":
        """Enforce uniqueness for paths/namespaces and local ports."""
        from urllib.parse import urlparse
        ports = []
        paths = []
        for ep in self.endpoints:
            if ep.path in paths:
                raise ValueError(f"Duplicate path detected: {ep.path}")
            paths.append(ep.path)
            
            # Resolve port from either explicit port (stdio_bridge) or parsed url (managed_cli)
            resolved_port = ep.port
            if ep.mode == "managed_cli" and ep.url:
                try:
                    parsed = urlparse(ep.url)
                    resolved_port = parsed.port
                except Exception:
                    pass
                    
            if resolved_port is not None:
                if resolved_port in ports:
                    raise ValueError(f"Duplicate port detected: {resolved_port}")
                ports.append(resolved_port)
        return self

class ConfigWatcher:
    """
    An asynchronous file watcher that polls config.yaml modification times.
    Triggers an event-safe async or sync callback when modifications are detected.
    """
    def __init__(self, config_path: str, callback: Callable[[RouterConfig], None], poll_interval: float = 1.0):
        self.config_path = config_path
        self.callback = callback
        self.poll_interval = poll_interval
        self._last_mtime: Optional[float] = None
        self._task: Optional[asyncio.Task] = None
        self._running = False

    async def start(self):
        self._running = True
        self._task = asyncio.create_task(self._poll_loop())
        logger.info(f"Started config polling for: {self.config_path}")

    async def stop(self):
        self._running = False
        if self._task:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
        logger.info("Stopped config polling.")

    async def _poll_loop(self):
        while self._running:
            try:
                if os.path.exists(self.config_path):
                    mtime = os.path.getmtime(self.config_path)
                    if self._last_mtime is None or mtime > self._last_mtime:
                        self._last_mtime = mtime
                        logger.info(f"Detected change in configuration: {self.config_path}")
                        try:
                            new_config = load_router_config(self.config_path)
                            if asyncio.iscoroutinefunction(self.callback):
                                await self.callback(new_config)
                            else:
                                self.callback(new_config)
                        except Exception as e:
                            logger.error(f"Failed to load or validate new config: {e}")
            except Exception as e:
                logger.error(f"Error in config watcher poll loop: {e}")
            await asyncio.sleep(self.poll_interval)
