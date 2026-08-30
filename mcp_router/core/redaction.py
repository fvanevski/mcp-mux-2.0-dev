from __future__ import annotations

import re
from collections.abc import Iterable

from mcp_router.core.config_loader import RouterConfig

_REDACTED = "[REDACTED]"
_SECRET_NAME = re.compile(r"(?:authorization|cookie|token|secret|password|api[-_]?key)", re.IGNORECASE)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:authorization|cookie|token|secret|password|api[-_]?key)[A-Za-z0-9_.-]*)"
    r"(\s*[:=]\s*)([^\r\n]+)"
)


class SecretRedactor:
    def __init__(self, values: Iterable[str] = ()) -> None:
        unique = {value for value in values if len(value) >= 4}
        expanded = set(unique)
        for value in unique:
            scheme, separator, token = value.partition(" ")
            if separator and scheme.casefold() in {"bearer", "basic"} and len(token.strip()) >= 4:
                expanded.add(token.strip())
        self._values = tuple(sorted(expanded, key=len, reverse=True))

    @classmethod
    def from_router_config(cls, config: RouterConfig) -> SecretRedactor:
        values: list[str] = []
        if config.security.api_key is not None:
            values.append(config.security.api_key.get_secret_value())
        for endpoint in config.endpoints:
            if endpoint.headers:
                for name, value in endpoint.headers.items():
                    if _SECRET_NAME.search(name):
                        values.append(value)
            if endpoint.mode == "managed_cli":
                for name, value in endpoint.env.items():
                    if _SECRET_NAME.search(name):
                        values.append(value)
        return cls(values)

    def redact(self, text: str) -> str:
        output = text
        for value in self._values:
            output = output.replace(value, _REDACTED)
        output = _SECRET_ASSIGNMENT.sub(
            lambda match: f"{match.group(1)}{match.group(2)}{_REDACTED}",
            output,
        )
        return output
