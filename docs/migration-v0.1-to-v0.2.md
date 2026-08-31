# Migration: v0.1.x to v0.2.0

v0.2.0 is a protocol/security hardening release. Existing deployments should review every item below before upgrading.

## Runtime dependency

The supported Python MCP SDK line is current stable v2. The v0.2.0 manifest requires `mcp>=2.1.1,<3`; the release lock resolves `mcp 2.1.1`. Python 3.13 and 3.14 are CI-supported.

## Intentional behavior changes

1. **Malformed JSON-RPC is rejected.** The mux no longer repairs requests missing `jsonrpc: "2.0"`.
2. **Streamable HTTP batches are rejected.** Send one JSON-RPC request object per HTTP request.
3. **Modern `2026-07-28` metadata is enforced.** Requests require the protocol/client-capabilities `_meta` envelope and matching `MCP-Protocol-Version`.
4. **Modern routing headers are enforced.** `Mcp-Method` must match the body method; named operations require a matching `Mcp-Name` derived from `params.name` or resource `params.uri`.
5. **Modern namespace subpaths are removed.** Canonical modern traffic uses the bare `/<namespace>` route. Arbitrary subpath rewriting remains legacy-only.
6. **Direct capability authorization is enforced.** A hidden/denied capability cannot be invoked directly merely because list projection hid it.
7. **Caller and upstream credentials are isolated.** Caller authorization, cookies, proxy authorization, and forwarded identity are not passed upstream; configured endpoint credentials remain authoritative.
8. **HTTP exposure is hardened.** Host, Origin, loopback/authenticated binding, trusted-proxy identity, and CORS behavior are validated.
9. **Managed runtime lifecycle is explicit.** Startup, running, draining, failure, leases, restart policy, inactivity, reload, and cleanup are represented by endpoint runtime state rather than loose process heuristics.
10. **The local legacy SSE bridge is bounded and deprecated.** It is disabled unless explicitly configured, and its queue, TTL, session count, backpressure, cancellation, retirement, and failure behavior are bounded/observable.
11. **Malformed/failing upstream responses fail closed.** An upstream response labeled `application/json` must be valid UTF-8 and satisfy strict JSON syntax, including rejection of non-standard numeric constants such as `NaN`, `Infinity`, and `-Infinity`. Invalid UTF-8 is not repaired with replacement characters and invalid JSON is not forwarded as a successful MCP payload; the canonical gateway returns its own HTTP 502 error and records the upstream failure, while the legacy bridge terminates the affected SSE session as an upstream-response failure. A post-header buffered JSON read failure is likewise converted to a gateway 502 before forwarding; failures after a streaming response has begun are counted, upstream context is closed, and the stream terminates rather than silently completing.
12. **Automatic trace propagation is deliberately narrow.** Valid W3C version-00 `traceparent` plus bounded `tracestate` are forwarded by default; arbitrary caller `baggage` is not. Operators that intentionally need another non-security header must opt it in through endpoint `inbound_headers` after reviewing its trust implications.
13. **`stdio_bridge` is rejected.** Only implemented endpoint modes are accepted; no incomplete stdio compatibility mode is advertised.

## Configuration review

Validate every endpoint against `docs/configuration.md`. In particular, migrate managed shell commands to structured `argv` where possible, ensure modern endpoints use `transport: streamable-http`, add `legacy_sse_bridge` only for a known legacy consumer, and configure authenticated security before any non-loopback bind.

## Operational changes

- `/summary` now includes runtime and gateway counters.
- `/metrics` exposes endpoint state, active leases, process restart attempts, stream cancellations, denied calls, upstream errors, and bounded legacy-bridge counters.
- Requests emit payload-free structured operational logs and return `X-Request-Id`.
- Valid supported W3C version-00 `traceparent` and bounded `tracestate` are forwarded upstream without logging raw trace values; caller `baggage` is deny-by-default.

## Validation changes

PR validation is split into named Python 3.13/3.14 quality, unit, integration, dependency-audit, and official MCP `2026-07-28` conformance jobs. The deterministic local assessment runner remains exact-SHA host evidence and does not itself make merge or release decisions.
