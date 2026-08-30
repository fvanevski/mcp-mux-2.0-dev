from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from typing import Any

from mcp_router.core.config_loader import Endpoint


@dataclass(frozen=True)
class PolicyDecision:
    allowed: bool
    principal: str
    endpoint: str
    method: str
    name: str | None
    reason: str | None = None


@dataclass(frozen=True)
class CapabilityPolicy:
    allowed_methods: frozenset[str] | None = None
    denied_methods: frozenset[str] | None = None
    allowed_tools: frozenset[str] | None = None
    denied_tools: frozenset[str] | None = None
    allowed_resources: frozenset[str] | None = None
    denied_resources: frozenset[str] | None = None
    allowed_prompts: frozenset[str] | None = None
    denied_prompts: frozenset[str] | None = None

    @classmethod
    def from_endpoint(cls, endpoint: Endpoint) -> CapabilityPolicy:
        return cls(
            allowed_methods=_freeze(endpoint.allowed_methods),
            denied_methods=_freeze(endpoint.denied_methods),
            allowed_tools=_freeze(endpoint.allowed_tools),
            denied_tools=_freeze(endpoint.denied_tools),
            allowed_resources=_freeze(endpoint.allowed_resources),
            denied_resources=_freeze(endpoint.denied_resources),
            allowed_prompts=_freeze(endpoint.allowed_prompts),
            denied_prompts=_freeze(endpoint.denied_prompts),
        )

    @property
    def version(self) -> str:
        payload = {
            "allowed_methods": _sorted_or_none(self.allowed_methods),
            "denied_methods": _sorted_or_none(self.denied_methods),
            "allowed_tools": _sorted_or_none(self.allowed_tools),
            "denied_tools": _sorted_or_none(self.denied_tools),
            "allowed_resources": _sorted_or_none(self.allowed_resources),
            "denied_resources": _sorted_or_none(self.denied_resources),
            "allowed_prompts": _sorted_or_none(self.allowed_prompts),
            "denied_prompts": _sorted_or_none(self.denied_prompts),
        }
        encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode()
        return hashlib.sha256(encoded).hexdigest()

    @property
    def has_discovery_projection(self) -> bool:
        return any(
            value is not None
            for value in (
                self.allowed_tools,
                self.denied_tools,
                self.allowed_resources,
                self.denied_resources,
                self.allowed_prompts,
                self.denied_prompts,
            )
        )

    def authorize(
        self,
        *,
        principal: str,
        endpoint: str,
        method: str,
        name: str | None,
    ) -> PolicyDecision:
        if self.allowed_methods is not None and method not in self.allowed_methods:
            return PolicyDecision(False, principal, endpoint, method, name, "method is not allowed")
        if self.denied_methods is not None and method in self.denied_methods:
            return PolicyDecision(False, principal, endpoint, method, name, "method is denied")

        allowed_names: frozenset[str] | None = None
        denied_names: frozenset[str] | None = None
        if method == "tools/call":
            allowed_names, denied_names = self.allowed_tools, self.denied_tools
        elif (
            method in {"resources/read", "resources/subscribe", "resources/unsubscribe"}
            or method == "subscriptions/listen" and name is not None
        ):
            allowed_names, denied_names = self.allowed_resources, self.denied_resources
        elif method == "prompts/get":
            allowed_names, denied_names = self.allowed_prompts, self.denied_prompts

        if allowed_names is not None and (name is None or name not in allowed_names):
            return PolicyDecision(False, principal, endpoint, method, name, "capability is not allowed")
        if denied_names is not None and name is not None and name in denied_names:
            return PolicyDecision(False, principal, endpoint, method, name, "capability is denied")
        return PolicyDecision(True, principal, endpoint, method, name)

    def project_json_text(
        self,
        line_or_body: str,
        *,
        principal: str,
        endpoint: str,
    ) -> tuple[str, bool]:
        prefix = ""
        json_text = line_or_body
        if line_or_body.startswith("data: "):
            prefix = "data: "
            json_text = line_or_body[6:].strip()

        try:
            payload = json.loads(json_text)
        except (json.JSONDecodeError, TypeError):
            return line_or_body, False
        if not isinstance(payload, dict):
            return line_or_body, False
        result = payload.get("result")
        if not isinstance(result, dict):
            return line_or_body, False

        changed = False
        for key, method, name_key in (
            ("tools", "tools/call", "name"),
            ("resources", "resources/read", "uri"),
            ("prompts", "prompts/get", "name"),
        ):
            items = result.get(key)
            if not isinstance(items, list):
                continue
            projected: list[Any] = []
            list_changed = False
            for item in items:
                if not isinstance(item, dict):
                    projected.append(item)
                    continue
                raw_name = item.get(name_key)
                name = raw_name if isinstance(raw_name, str) and raw_name else None
                decision = self.authorize(
                    principal=principal,
                    endpoint=endpoint,
                    method=method,
                    name=name,
                )
                if decision.allowed:
                    projected.append(item)
                else:
                    list_changed = True
            if list_changed:
                result[key] = projected
                changed = True

        if not changed:
            return line_or_body, False
        return f"{prefix}{json.dumps(payload, separators=(',', ':'))}", True


def _freeze(value: list[str] | None) -> frozenset[str] | None:
    return None if value is None else frozenset(value)


def _sorted_or_none(value: frozenset[str] | None) -> list[str] | None:
    return None if value is None else sorted(value)
