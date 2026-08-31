# Deployment hardening

## Network exposure

The default deployment is loopback-only. Keep `--host 127.0.0.1` unless remote access is required. A non-loopback bind is rejected unless `security.mode: authenticated` is configured.

For authenticated exposure:

1. define explicit `allowed_hosts` and `allowed_origins`;
2. configure one supported authentication boundary (API key or a trusted reverse-proxy identity boundary);
3. enumerate `trusted_proxies` narrowly; never trust arbitrary forwarded identity headers;
4. terminate TLS at a controlled reverse proxy or upstream network boundary;
5. firewall the mux and managed child ports from unintended networks.

## Secrets

- Supply upstream credentials through environment expansion rather than committing literal secrets.
- Keep caller credentials distinct from configured upstream credentials; the gateway strips caller authorization/cookie/proxy-authorization headers before forwarding.
- Do not put secrets in endpoint paths, tool names, resource URIs, summaries, or other fields intended for operational visibility.
- Treat configuration write access as privileged, especially when `unsafe_shell_command` is used.

## Managed processes

Prefer `argv` over `unsafe_shell_command`. Use explicit `cwd`, minimal environment variables, bounded readiness timeouts, restart policy, inactivity timeout, and least-privilege service credentials. Managed process ports should normally bind only to loopback.

## Observability

`/summary` provides endpoint descriptions and runtime/compatibility state. `/metrics` exposes operational counters and runtime state without request payloads or credentials. Structured request logs include request ID, endpoint, protocol revision, method, named capability, status, duration, bytes streamed, policy outcome, trace-context presence, and cancellation state.

Request IDs are returned as `X-Request-Id`. A caller-provided value is accepted only when it is a bounded token; otherwise the gateway creates one. Valid W3C version-00 trace context is propagated upstream but raw trace values are not logged.

## Validation before deployment

Use the exact repository lock and the current CI/assessment authority. A release candidate should have green Python 3.13/3.14 quality, unit, integration, dependency-audit, and MCP conformance checks. Host evidence or a single green job is not a substitute for exact-head PR review or release-gate disposition.
