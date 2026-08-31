# v0.2.0 release notes

## Highlights

- Implements and validates the MCP `2026-07-28` stateless Streamable HTTP gateway contract while retaining explicit legacy compatibility paths.
- Enforces direct capability policy, credential isolation, Host/Origin/authentication boundaries, fail-closed protocol/routing validation, and containment of invalid-UTF-8, malformed-framing, or non-standard-constant JSON responses from upstreams without lossy repair/acceptance.
- Reworks managed endpoint lifecycle around explicit runtime state, leases, bounded restart policy, transactional reload, and deterministic cleanup.
- Hardens and bounds the deprecated local legacy SSE compatibility adapter.
- Adds payload-free structured request logs, request IDs, supported trace-context propagation, `/metrics`, and expanded `/summary` operational state.
- Reorganizes the test suite into focused configuration, protocol, policy, proxy, streaming, process, reload, security, and integration coverage, including deterministic malformed-JSON, invalid-UTF-8 JSON, invalid-JSON-constant, HTTP-failure, and transport-failure upstream fixtures; buffered and midstream response-read failure regressions; and bounded repeated bridge disconnect/hot-reload leak regression.
- Adds Python 3.13/3.14 CI, Ruff, Pyrefly, unit/integration jobs, `pip-audit 2.10.1`, and official MCP conformance `0.2.0-alpha.11` against the Python MCP SDK 2.1.1 Everything server pinned at commit `0921d94a74db900dccd2d534842aa7b6160542d2`.

## Dependency baseline

- Python package version: `0.2.0`
- MCP Python SDK: `mcp>=2.1.1,<3` (release lock: `2.1.1`)
- Official conformance runner: `@modelcontextprotocol/conformance@0.2.0-alpha.11`
- Official Everything test server: Python MCP SDK 2.1.1 `mcp-everything-server`, source pinned at `0921d94a74db900dccd2d534842aa7b6160542d2`
- Referee baseline: exactly `server-stateless:sep-2575-server-unsupported-version-error`, limited to fixture-internal metrics correlation for gateway-terminated unsupported-version traffic

## Breaking changes

See `docs/migration-v0.1-to-v0.2.md`. The release intentionally rejects malformed/batched modern traffic, fails closed on malformed upstream JSON, narrows automatic trace propagation to validated `traceparent` plus bounded `tracestate`, enforces the `2026-07-28` metadata/routing contract, removes arbitrary modern subpaths, enforces direct capability authorization and credential isolation, hardens HTTP exposure, replaces process/reload heuristics with explicit runtime lifecycle, bounds/deprecates the local SSE bridge, and rejects unimplemented `stdio_bridge` configuration.

## Release evidence

The release tag must not be created from this document alone. Gate D owns the final exact-main CI, conformance, documentation, migration, version, and release-readiness disposition for `v0.2.0`.
