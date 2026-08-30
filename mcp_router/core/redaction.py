from __future__ import annotations

import re
from collections.abc import Iterable

from mcp_router.core.config_loader import RouterConfig

_REDACTED = "[REDACTED]"
_SECRET_NAME = re.compile(
    r"(?:authorization|credential|cookie|token|secret|password|api[-_]?key|(?:^|[-_.])auth(?:$|[-_.]))",
    re.IGNORECASE,
)
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b([A-Za-z0-9_.-]*(?:authorization|cookie|token|secret|password|api[-_]?key)[A-Za-z0-9_.-]*)"
    r"(\s*[:=]\s*)([^\r\n]+)"
)


class SecretRedactor:
    def __init__(self, values: Iterable[str] = ()) -> None:
        unique = {value for value in values if value}
        expanded = set(unique)
        for value in unique:
            scheme, separator, token = value.partition(" ")
            if separator and scheme.casefold() in {"bearer", "basic"} and token.strip():
                expanded.add(token.strip())
        self._values = tuple(sorted(expanded, key=len, reverse=True))

    @property
    def active(self) -> bool:
        return bool(self._values)

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

    def stream(self) -> StreamingSecretRedactor:
        return StreamingSecretRedactor(self)


class StreamingSecretRedactor:
    """Redact known secrets without exposing values split across text chunks."""

    def __init__(self, redactor: SecretRedactor) -> None:
        self._redactor = redactor
        self._carry = ""
        self._max_literal_length = max((len(value) for value in redactor._values), default=0)

    def feed(self, text: str) -> str:
        if not text:
            return ""
        if self._max_literal_length <= 1:
            return self._redactor.redact(text)

        combined = self._carry + text
        keep = self._max_literal_length - 1
        if len(combined) <= keep:
            self._carry = combined
            return ""

        cutoff = len(combined) - keep
        while True:
            adjusted = cutoff
            for value in self._redactor._values:
                start = combined.find(value)
                while start >= 0:
                    end = start + len(value)
                    if start < cutoff < end:
                        adjusted = min(adjusted, start)
                    start = combined.find(value, start + 1)
            if adjusted == cutoff:
                break
            cutoff = adjusted

        output = self._redactor.redact(combined[:cutoff])
        self._carry = combined[cutoff:]
        return output

    def finish(self) -> str:
        output = self._redactor.redact(self._carry)
        self._carry = ""
        return output
