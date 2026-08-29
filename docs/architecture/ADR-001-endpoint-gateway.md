# ADR-001: Preserve the endpoint-per-server MCP gateway architecture

- **Status:** Accepted
- **Decision date:** 2026-08-29
- **Phase:** Issue #3 — compatibility contract and architecture baseline
- **Frozen implementation base:** `0d630e7aab347657c7aca108451a0a6850793edb`

## Context

`mcp-mux` is currently an ASGI reverse proxy and managed-process orchestrator. Its public namespace is not an MCP tool namespace: each configured endpoint owns a distinct HTTP path such as `/firecrawl`, `/huggingface`, or `/context7`, and traffic under that path is forwarded to exactly one configured upstream.

This decision records that architecture from current source before later phases change protocol handling. It also fixes the target public contract for MCP `2026-07-28` traffic so later protocol work does not accidentally turn the mux into a monolithic MCP server.

The target protocol revision is intentionally different from the current implementation in several places. MCP `2026-07-28` is stateless at the protocol layer: every modern request carries its protocol version and client capabilities in `params._meta`, and Streamable HTTP mirrors protocol/method/name routing information into required HTTP headers. Modern processing must not depend on the legacy `initialize` handshake, connection history, or `Mcp-Session-Id` as protocol state. Later phases are responsible for enforcing those rules at the gateway edge; Phase 0 freezes the boundary and supplies a protocol-valid deterministic compatibility oracle.

## Current architecture from source

The following is source-derived behavior at the frozen base, not a restatement of README prose.

### Public routing

`mcp_router/server.py` constructs one `MCPRouter` and registers:

- `GET /summary`;
- `/{path_prefix}` for `GET`, `POST`, `PUT`, `DELETE`, and `OPTIONS`;
- `/{path_prefix}/{subpath:path}` for the same methods.

`MCPRouter.catch_all_proxy()` looks up `path_prefix` in `self._configs`. An unknown prefix returns `404` locally. A known prefix selects exactly one `EndpointConfig` and therefore one upstream target.

`get_target_url()` removes the mux namespace prefix and appends the remaining suffix to the configured upstream URL. For a configured upstream ending in `/mcp`, a request to the bare mux namespace therefore targets that single upstream MCP endpoint.

### Endpoint configuration

`mcp_router/core/config_loader.py` defines `EndpointConfig` and `RouterConfig`.

Current accepted endpoint modes are:

- `remote` — requires `url`;
- `managed_cli` — requires `command` and `url`;
- `stdio_bridge` — accepted by configuration when `command` and `port` exist, but not implemented end to end by the router.

Transport is explicitly configured or inferred as `streamable-http` when the URL ends in `/mcp` or contains `/mcp/`; otherwise it defaults to `sse`.

The current active `mcp_router/config.yaml` contains four endpoints whose URLs resolve to `streamable-http`: `web-search`, `firecrawl`, `huggingface`, and `context7`. `firecrawl` is the only active `managed_cli` endpoint. The commented Crawl4AI example is not an active compatibility commitment.

### Current Streamable HTTP path

For `transport="streamable-http"`:

- direct `POST` requests are forwarded to the configured upstream URL;
- incoming headers are forwarded except hop-by-hop/host/content-length exclusions, then endpoint-configured headers are overlaid;
- `POST` and `DELETE` force `Accept: application/json, text/event-stream` upstream;
- JSON responses are buffered, optionally tool-filtered, and returned after stale decoded representation headers are removed;
- event-stream responses are relayed through a streaming response;
- a plain `GET` that does not request SSE is rejected locally with `405`;
- `GET` with `Accept: text/event-stream` is forwarded upstream unless the endpoint explicitly enables `legacy_sse_bridge`.

The current implementation also repairs JSON-RPC request bodies by adding a missing `"jsonrpc": "2.0"` field to dictionary messages with a `method`, including items in batches. That repair is current behavior only and is explicitly scheduled for removal in Phase 2.

### Current legacy session behavior

There are two distinct legacy paths.

1. **Transparent legacy Streamable HTTP.** If a downstream client sends legacy `initialize` traffic or `Mcp-Session-Id` directly to a normal Streamable HTTP endpoint, the mux forwards those headers/bodies without creating local bridge state. Upstream response session headers are returned to the client.
2. **Opt-in local SSE bridge.** If `legacy_sse_bridge: true`, an SSE `GET` creates a local `BridgeSession`, emits an `event: endpoint` URI containing a local `session_id`, accepts later client `POST`s through that local session, and maps an upstream `Mcp-Session-Id` onto the local bridge session.

Bridge sessions are keyed globally but carry `path_prefix`. Cross-endpoint reuse is rejected with `409`, and `apply_configuration()` drops sessions when an endpoint is removed or its configuration changes.

No active endpoint in `mcp_router/config.yaml` sets `legacy_sse_bridge: true`. The operator-verified concrete-client inventory recorded on Issue #3 also identifies no client that requires the bridge. Phase 0 therefore records it as **provisionally unused by real clients**, pending deployment telemetry or new authoritative client evidence.

### Current legacy HTTP+SSE path

For `transport="sse"`:

- downstream `GET` with `Accept: text/event-stream` opens an upstream SSE stream;
- `data:` lines that contain an upstream message endpoint are rewritten to include the mux namespace;
- a later downstream `POST` beneath the namespace is rewritten back to the upstream message path.

This arbitrary subpath rewriting is a legacy compatibility behavior, not the modern canonical route contract.

### Current policy behavior

`filter_tools_response()` projects `tools/list` responses using `allowed_tools` or `denied_tools`. If both are configured, `allowed_tools` wins and `denied_tools` is cleared by validation.

This is discovery projection only. Current source does **not** reject a direct `tools/call` merely because the tool was filtered out of `tools/list`; the parent epic identifies that as a security defect for Phase 3.

### Current managed-process behavior

`mcp_router/core/process_manager.py` implements a singleton `ProcessManager`.

For `managed_cli` endpoints, `catch_all_proxy()` serializes on a per-path lock and lazily starts the process when the first request arrives. `ProcessManager.start_managed_server()` currently:

- starts the configured command through `asyncio.create_subprocess_shell()`;
- creates a Unix process group with `os.setsid` where available;
- consumes stdout/stderr in background tasks;
- waits for TCP port readiness before declaring the endpoint ready.

The router tracks `last_activity` and `active_connections`; the idle checker stops a managed process after the configured timeout when those heuristics indicate inactivity. Configuration changes schedule process stop operations and remove the old endpoint state. Later runtime work is responsible for replacing these heuristics with transactional endpoint runtime state and upstream-work leases.

## Decision

### 1. Keep one public namespace per configured upstream

The mux remains an endpoint-per-server gateway.

For an endpoint configured as `path: "firecrawl"`, public MCP traffic is addressed to `/firecrawl`. For `path: "huggingface"`, traffic is addressed to `/huggingface`, and so on. A namespace selects one upstream configuration and one policy/runtime boundary.

The mux **will not** import every upstream tool and republish them from a single merged MCP server namespace.

### 2. Canonical modern route is one Streamable HTTP endpoint per namespace

For MCP `2026-07-28`, the canonical public route is:

```text
POST /<namespace>
```

Modern requests are stateless and self-contained. Every request must supply the metadata needed to process it without depending on a local mux protocol session or prior request history. Phase 0's deterministic modern fixture is the compatibility oracle for a valid peer; Phase 2 owns strict validation in the production gateway.

For modern traffic:

- `/<namespace>` is the single MCP endpoint and each request is an independent `POST`;
- every request carries `params._meta["io.modelcontextprotocol/protocolVersion"] = "2026-07-28"` and `params._meta["io.modelcontextprotocol/clientCapabilities"]`; `clientInfo` is optional;
- Streamable HTTP requires `MCP-Protocol-Version` to match the body protocol version and `Mcp-Method` to match the JSON-RPC method;
- `Mcp-Name` mirrors `params.name` for `tools/call` and `prompts/get`, and `params.uri` for `resources/read`;
- successful modern results include `resultType`; `"complete"` represents a completed final result;
- arbitrary `/<namespace>/<subpath>` forwarding is not part of the modern contract;
- the legacy `initialize`/`initialized` handshake is not part of the modern contract;
- a legacy `Mcp-Session-Id` header must not become implicit modern protocol/session state;
- legacy `GET`, `DELETE`, held-open SSE, and message-subpath semantics remain compatibility concerns only where an explicitly supported legacy mode requires them.

Later protocol-edge work must reject malformed or inconsistent modern messages before forwarding while preserving valid unknown extension fields and `_meta` data.

### 3. Preserve supported legacy eras explicitly, not by implicit protocol repair

The gateway may support approved legacy Streamable HTTP and HTTP+SSE clients on the same endpoint-per-server architecture, but those behaviors must be explicit compatibility paths.

The modern path must not acquire local bridge state merely because legacy support exists elsewhere. General-purpose modern-to-legacy translation is not a goal.

### 4. Keep policy, credentials, runtime, and compatibility scoped to the endpoint

Because each namespace identifies one endpoint, later policy evaluation, upstream credential injection, runtime state, rate/concurrency limits, and legacy session state are scoped to that endpoint. This is the architectural boundary future phases should strengthen rather than bypass.

## Rejected alternative: one merged-tool MCP server

A monolithic server that discovers every upstream, imports all tools, and republishes them under one MCP tool namespace is rejected for this refactor.

That design would change the product contract rather than refactor the gateway. It would also collapse endpoint-specific credentials, upstream lifecycle, policy, failure isolation, compatibility handling, and transport semantics into a new aggregation layer. Tool-name collision/renaming policy would become mandatory, and upstream schema identity would no longer be preserved transparently.

Nothing in the v0.2.0 objective requires that change.

## Consequences

### Positive

- Existing endpoint URLs remain stable architectural identities.
- Upstream tools retain their native names and schemas.
- Credentials and policy remain separable by upstream endpoint.
- Managed-process lifecycle remains separable by endpoint.
- Modern MCP v2 validation can be added at the gateway edge without reimplementing every upstream server.
- Legacy compatibility can be isolated and retired independently.

### Constraints

- Clients must know which endpoint namespace exposes the upstream they intend to use.
- Cross-upstream tool discovery is not provided as one MCP `tools/list` response.
- `/summary` may describe available endpoint namespaces, but it is not a merged MCP capability server.
- Compatibility tests must exercise transport/protocol behavior per endpoint class rather than assuming one unified server.

## Intended v0.2.0 breaking changes

The following changes are already established by issues #4–#8 and are intentionally **not** implemented by this ADR. They are recorded here so the frozen baseline is not mistaken for the final contract.

1. Replace stale/FastMCP dependency surfaces with the official MCP v2 dependency and a typed configuration foundation.
2. Stop repairing malformed JSON-RPC; reject malformed objects and Streamable HTTP batches locally.
3. Require/validate modern `MCP-Protocol-Version`, `Mcp-Method`, and applicable `Mcp-Name` semantics before forwarding.
4. Restrict arbitrary namespace subpaths to explicitly configured legacy HTTP+SSE behavior.
5. Make modern `2026-07-28` requests stateless and independent of bridge-session state.
6. Enforce `Host`/`Origin` and explicit caller-authentication policy; remove wildcard credentialed CORS.
7. Stop forwarding inbound gateway `Authorization` upstream by default; separate caller credentials from injected upstream credentials.
8. Enforce tool/method/name policy before upstream calls, including direct `tools/call` denial.
9. Replace connection-count/idle heuristics and racy reload behavior with explicit endpoint runtime state and upstream-work leases.
10. Make shell-based managed-process execution an explicit unsafe escape hatch rather than the implicit default.
11. Isolate and bound the optional legacy SSE adapter, with observable utilization and deterministic cleanup.
12. Either implement `stdio_bridge` end to end or reject it in configuration until supported.

## Out of scope

The following remain outside the v0.2.0 refactor unless a later issue explicitly changes scope:

- merging all upstream tools into one MCP namespace;
- renaming upstream tools or adapting their schemas as a product feature;
- general protocol translation between arbitrary modern and legacy revisions;
- adding Tasks, MCP Apps, or other MCP features solely because the protocol revision supports them;
- sharing policy-filtered capability caches across principals;
- introducing cross-endpoint application state that defeats endpoint isolation.

## Verification contract

`docs/compatibility-matrix.md` is the companion claim-to-test matrix. Phase 0 adds deterministic in-process upstream fixtures for:

- modern stateless Streamable HTTP;
- legacy sessionful Streamable HTTP;
- legacy HTTP+SSE.

Existing regressions continue to cover the opt-in local SSE bridge, remote-session mapping, cross-endpoint session isolation, configuration-driven session cleanup, response filtering, and current JSON-RPC repair behavior.

## References

Repository authority:

- Issue #1 — endpoint-per-server architectural direction and epic non-goals.
- Issue #3 — this Phase 0 baseline.
- Issue #5 — strict dual-era protocol edge and canonical modern route.
- Issue #6 — endpoint-scoped security and capability policy.
- Issue #7 — endpoint runtime and managed-process supervision.
- Issue #8 — isolated legacy compatibility and stdio decision.
- `mcp_router/server.py` — current routing, transport, bridge, session, and filtering behavior.
- `mcp_router/core/config_loader.py` — current endpoint schema and transport inference.
- `mcp_router/core/process_manager.py` — current managed-process lifecycle.
- `mcp_router/config.yaml` — current active upstream inventory.
- `tests/test_router.py` — current behavioral regressions.

External protocol authority:

- MCP `2026-07-28` base protocol: <https://modelcontextprotocol.io/specification/2026-07-28/basic>
- MCP `2026-07-28` Streamable HTTP transport: <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http>
- MCP `2026-07-28` discovery: <https://modelcontextprotocol.io/specification/2026-07-28/server/discover>
- MCP `2026-07-28` tools: <https://modelcontextprotocol.io/specification/2026-07-28/server/tools>
- Release announcement (supplemental): <https://blog.modelcontextprotocol.io/posts/2026-07-28/>
