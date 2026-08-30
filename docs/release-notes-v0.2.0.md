# v0.2.0 release notes

## Highlights

- Implements and validates the MCP `2026-07-28` stateless Streamable HTTP gateway contract while retaining explicit legacy compatibility paths.
- Enforces direct capability policy, credential isolation, Host/Origin/authentication boundaries, and fail-closed protocol/routing validation.
- Reworks managed endpoint lifecycle around explicit runtime state, leases, bounded restart policy, transactional reload, and deterministic cleanup.
- Hardens and bounds the deprecated local legacy SSE compatibility adapter.
- Adds payload-free structured request logs, request IDs, supported trace-context propagation, `/metrics`, and expanded `/summary` operational state.
- Reorganizes the test suite into focused configuration, protocol, policy, proxy, streaming, process, reload, security, and integration coverage.
- Adds Python 3.13/3.14 CI, Ruff, Pyrefly, unit/integration jobs, `pip-audit 2.10.1`, and official MCP conformance `v0.1.16` against a proxied official Everything server.

## Dependency baseline

- Python package version: `0.2.0`
- MCP Python SDK: `mcp>=2.1.1,<3` (release lock: `2.1.1`)
- Official conformance runner: `@modelcontextprotocol/conformance@0.1.16`
- Official Everything test server: `@modelcontextprotocol/server-everything@2.0.0`

## Breaking changes

See `docs/migration-v0.1-to-v0.2.md`. The release intentionally rejects malformed/batched modern traffic, enforces the `2026-07-28` metadata/routing contract, removes arbitrary modern subpaths, enforces direct capability authorization and credential isolation, hardens HTTP exposure, replaces process/reload heuristics with explicit runtime lifecycle, bounds/deprecates the local SSE bridge, and rejects unimplemented `stdio_bridge` configuration.

## Release evidence

The release tag must not be created from this document alone. Gate D owns the final exact-main CI, conformance, documentation, migration, version, and release-readiness disposition for `v0.2.0`.
