# MCP compatibility baseline and claim-to-test matrix

- **Phase:** Issue #3 — compatibility contract and architecture baseline
- **Frozen implementation base:** `0d630e7aab347657c7aca108451a0a6850793edb`
- **Architecture decision:** `docs/architecture/ADR-001-endpoint-gateway.md`

## Purpose

This document separates three things that later phases must not conflate:

1. **current behavior** that exists at the frozen base;
2. **target modern MCP `2026-07-28` semantics** already established by the epic/Phase 2 contract;
3. **legacy compatibility paths** that remain supported only when explicitly represented in this matrix.

The tests named below are the baseline falsification points for later refactors. A later change that intentionally alters a baseline claim must update both the implementation contract and this matrix rather than silently rewriting the test around the new behavior.

## Evidence boundary and deployment inventory

### Concrete downstream clients

`REAL_CLIENT_INVENTORY=UNVERIFIED`

Issue #3, its comments, the current source, repository configuration, README, and current tests do **not** identify concrete downstream client products/applications by name. This repository therefore cannot truthfully claim that a named product such as a particular editor, agent harness, desktop application, or SDK is a required compatibility consumer.

Phase 0 freezes **protocol client classes** instead:

- modern stateless MCP `2026-07-28` over Streamable HTTP;
- legacy sessionful Streamable HTTP using `initialize` and `Mcp-Session-Id`;
- legacy HTTP+SSE using an SSE endpoint plus a message POST endpoint;
- the mux-specific opt-in `legacy_sse_bridge` adapter for clients that require that local endpoint flow.

A deployment owner may later add concrete client identities without changing the protocol claims. Until then, the literal real-client deployment inventory remains an explicit evidence gap, not an inferred fact.

### Active upstreams declared by the repository

The current `mcp_router/config.yaml` establishes these active upstream identities:

| Scenario ID | Namespace | Mode | Configured upstream | Current inferred transport | Phase 0 test identity |
|---|---|---|---|---|---|
| `modern-remote-web-search` | `web-search` | `remote` | `https://mcp.garion.us/mcp` | `streamable-http` | `test_configured_upstreams_are_frozen_as_streamable_http[modern-remote-web-search]` |
| `modern-managed-firecrawl` | `firecrawl` | `managed_cli` | `http://localhost:3033/mcp` | `streamable-http` | `test_configured_upstreams_are_frozen_as_streamable_http[modern-managed-firecrawl]` |
| `modern-remote-huggingface` | `huggingface` | `remote` | `https://huggingface.co/mcp` | `streamable-http` | `test_configured_upstreams_are_frozen_as_streamable_http[modern-remote-huggingface]` |
| `modern-remote-context7` | `context7` | `remote` | `https://mcp.context7.com/mcp` | `streamable-http` | `test_configured_upstreams_are_frozen_as_streamable_http[modern-remote-context7]` |

The commented Crawl4AI block is an example of `transport: sse`; it is not active configuration and is therefore not treated as a currently deployed support commitment.

The deterministic mock upstreams do not make network calls to any of these services. They freeze the mux contract independently of upstream availability, credentials, or service drift.

## Legacy bridge consumer inventory

`LEGACY_SSE_BRIDGE_CONSUMERS=PROVISIONALLY_UNUSED`

Evidence:

- `legacy_sse_bridge` defaults to `false` in `EndpointConfig`;
- no active endpoint in `mcp_router/config.yaml` enables it;
- current coverage exercises the adapter only through tests.

This status means **no real consumer is established by repository evidence**. It does not prove that no external deployment uses the bridge. Phase 5 is expected to add utilization telemetry before any eventual removal decision.

## Canonical target contract for modern MCP `2026-07-28`

This section is the approved target contract for later implementation, not a claim that the current router already enforces every item.

| Claim | Target contract |
|---|---|
| Public route | One canonical MCP endpoint per namespace: `POST /<namespace>`. |
| Session model | Stateless at the MCP protocol layer; no local mux protocol session for modern requests. |
| Handshake | No legacy `initialize`/`initialized` requirement for modern traffic. |
| Session header | `Mcp-Session-Id` is legacy-only and not part of modern semantics. |
| Protocol identity | `MCP-Protocol-Version: 2026-07-28` is required/validated by the future protocol edge. |
| Routing headers | `Mcp-Method` must agree with the JSON-RPC method; `Mcp-Name` must agree for named operations such as `tools/call`, `resources/read`, and `prompts/get`. |
| Message shape | Valid JSON-RPC only; no request-body repair and no Streamable HTTP batches. |
| Transparency | Unknown extension methods, unknown fields, and `_meta` are preserved when valid. |
| Subpaths | Arbitrary `/<namespace>/<subpath>` forwarding is not part of the modern contract. |
| GET/DELETE/SSE | Compatibility-only where an approved legacy mode requires them; not a mechanism for introducing modern local session state. |
| Aggregation | No merged-tool MCP server; namespace still selects one upstream. |

## Claim-to-test matrix

### A. Modern stateless Streamable HTTP

| Claim ID | Baseline claim | Current/target classification | Test evidence |
|---|---|---|---|
| `M-ST-01` | `POST /<namespace>` maps to the configured upstream MCP endpoint. | Current + target | `test_modern_stateless_streamable_http_baseline` |
| `M-ST-02` | Valid modern protocol/routing headers pass through to the upstream. | Current pass-through; later strict validation | `test_modern_stateless_streamable_http_baseline` |
| `M-ST-03` | Two modern requests can complete without local bridge sessions or `Mcp-Session-Id`. | Current + target | `test_modern_stateless_streamable_http_baseline` |
| `M-ST-04` | Endpoint configuration overlays remain endpoint-specific and the configured active `/mcp` upstream inventory stays explicit. | Current architecture | `test_configured_upstreams_are_frozen_as_streamable_http[...]` |
| `M-ST-05` | `Accept` is currently normalized upstream to `application/json, text/event-stream` for Streamable HTTP POST. | Current behavior; later transport implementation may change mechanics while preserving compatibility | `test_modern_stateless_streamable_http_baseline`; existing `test_streamable_http_direct_post_accepts_json_only_clients` |
| `M-ST-06` | Modern target semantics reject local session dependence, malformed JSON-RPC, batches, and routing-header mismatches before forwarding. | Target only — Phase 2 | Future Phase 2 regressions; not falsely asserted as current behavior |

### B. Legacy sessionful Streamable HTTP

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `L-SH-01` | Legacy `initialize` can be forwarded to a Streamable HTTP upstream. | Current legacy compatibility | `test_legacy_sessionful_streamable_http_baseline` |
| `L-SH-02` | An upstream `Mcp-Session-Id` response header is returned downstream. | Current legacy compatibility | `test_legacy_sessionful_streamable_http_baseline` |
| `L-SH-03` | A later downstream `Mcp-Session-Id` is forwarded upstream transparently without creating a local bridge session. | Current legacy compatibility | `test_legacy_sessionful_streamable_http_baseline` |
| `L-SH-04` | Legacy session traffic remains endpoint-scoped. | Current invariant | Existing `test_streamable_http_rejects_session_for_different_endpoint` covers mux-owned bridge sessions; transparent legacy session traffic remains naturally scoped by namespace routing |

### C. Legacy HTTP+SSE

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `L-SSE-01` | An explicit `transport="sse"` endpoint proxies downstream SSE GET to the configured upstream SSE path. | Current legacy compatibility | `test_legacy_http_sse_baseline` |
| `L-SSE-02` | Upstream `event: endpoint` message paths are rewritten beneath the mux namespace. | Current legacy compatibility | `test_legacy_http_sse_baseline` |
| `L-SSE-03` | A downstream POST to that rewritten namespace subpath maps back to the upstream legacy message path and query. | Current legacy compatibility | `test_legacy_http_sse_baseline`; existing `test_sse_message_post_uses_upstream_message_path` |
| `L-SSE-04` | Arbitrary subpath rewriting is legacy-only in the target architecture. | Target constraint | Phase 2 regression required when route restriction is implemented |

### D. Mux-local `legacy_sse_bridge`

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `BR-01` | The local bridge is opt-in; normal Streamable HTTP SSE GET does not create local bridge state. | Current + target | Existing `test_streamable_http_sse_get_preserves_upstream_streamable_http` |
| `BR-02` | Enabling `legacy_sse_bridge` creates a local SSE bridge session and local endpoint event. | Current legacy compatibility | Existing `test_streamable_http_sse_get_opens_local_sse_bridge_when_enabled`; `test_streamable_http_bridge` |
| `BR-03` | The bridge captures upstream `Mcp-Session-Id` and reuses it for later bridge POSTs. | Current legacy compatibility | Existing `test_streamable_http_direct_response_json` |
| `BR-04` | Cross-endpoint local bridge session reuse is rejected. | Preservation invariant | Existing `test_streamable_http_rejects_session_for_different_endpoint` |
| `BR-05` | Removing/changing an endpoint drops its bridge sessions. | Current reload invariant | Existing `test_apply_configuration_drops_sessions_for_removed_or_changed_endpoint` |
| `BR-06` | Bridge queues/tasks/streams become bounded, cancellable, and observable. | Target only — Phase 5 | Future Phase 5 regressions |

### E. Current protocol repair and response projection

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `CUR-01` | Current router adds missing `jsonrpc: "2.0"` to request dictionaries with a `method`. | Current behavior scheduled to break | Existing `test_streamable_http_direct_post_adds_missing_jsonrpc_version` |
| `CUR-02` | Current router also repairs qualifying items inside batches. | Current behavior scheduled to break | Existing `test_streamable_http_direct_post_adds_missing_jsonrpc_version_to_batch_items` |
| `CUR-03` | Current tool policy projects `tools/list` using allow/deny lists. | Current behavior to be retained through a stronger policy layer | Existing `test_filter_tools_response_sse`, `test_filter_tools_response_json` |
| `CUR-04` | Current tool projection does not prove direct-call authorization. | Known defect, not a supported security claim | Phase 3 must add negative direct-call tests |
| `CUR-05` | Decoded JSON response transformation strips stale content encoding/length. | Current response-safety behavior | Existing `test_proxy_strips_encoding_headers_from_decoded_json_response` |

### F. Managed and remote endpoint behavior

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `RUN-01` | Remote endpoints proxy without local process startup. | Current architecture | Modern/legacy remote compatibility tests above |
| `RUN-02` | `managed_cli` endpoints are a distinct endpoint mode and current `firecrawl` inventory remains explicitly managed. | Current architecture | `test_configured_upstreams_are_frozen_as_streamable_http[modern-managed-firecrawl]`; existing config tests |
| `RUN-03` | Managed process startup currently uses the configured command and waits for TCP port readiness. | Current behavior scheduled for redesign | Existing `test_process_manager_lifecycle` |
| `RUN-04` | Runtime reload/idle behavior will become transactional and lease-based. | Target only — Phase 4 | Future Phase 4 concurrency/integration regressions |

## Named compatibility scenarios

The following names are the stable scenario vocabulary for this refactor:

- `modern-stateless-streamable-http`
- `legacy-sessionful-streamable-http`
- `legacy-http-sse`
- `legacy-local-sse-bridge-opt-in`
- `modern-remote-web-search`
- `modern-managed-firecrawl`
- `modern-remote-huggingface`
- `modern-remote-context7`

Concrete client-product names can be appended to these scenario classes when deployment evidence exists. They must not replace the protocol-class names, because the protocol claims are what the deterministic regression fixtures exercise.

## Deterministic mock upstream fixtures

`tests/fixtures/mock_upstream.py` provides an in-process `httpx.MockTransport` upstream with three modes:

| Fixture mode | Purpose | Network/service dependency |
|---|---|---|
| `modern-stateless` | Requires MCP `2026-07-28` protocol/method/name headers where applicable and rejects any `Mcp-Session-Id`. | None |
| `legacy-sessionful` | Returns a deterministic session ID from `initialize` and requires it on later requests. | None |
| `legacy-http-sse` | Emits a deterministic SSE endpoint event and accepts the corresponding message POST path. | None |

The fixtures intentionally avoid live Hugging Face, Context7, Firecrawl, or other network dependencies. They are compatibility fixtures, not conformance servers.

## Preservation invariants for later phases

```text
PRESERVATION_INVARIANTS:
- One configured namespace selects exactly one upstream endpoint/runtime boundary.
- Modern MCP v2 remains stateless through the mux.
- Approved legacy sessionful Streamable HTTP can remain transparent until explicitly deprecated.
- Legacy HTTP+SSE subpath rewriting exists only on an explicit legacy transport path.
- The mux-local legacy SSE bridge remains opt-in and endpoint/session isolated.
- Upstream tools retain their names and schemas; the mux does not become a merged-tool server.
- Remote and managed endpoint identities remain distinguishable.
- Compatibility tests do not depend on live external services.
- Known current defects are recorded as defects, not promoted into target guarantees.
```

## Intended breaking changes tracked for v0.2.0

The baseline intentionally anticipates these later changes:

- malformed JSON-RPC and batches become local errors instead of being repaired;
- modern routing headers/version become validated requirements;
- arbitrary modern namespace subpaths are removed;
- direct capability policy is enforced before forwarding;
- gateway/caller credentials are isolated from upstream credentials;
- insecure wildcard credentialed CORS and unvalidated Host/Origin behavior are removed;
- managed-process execution/reload/readiness semantics become explicit and deterministic;
- local legacy bridge resources become bounded and observable;
- `stdio_bridge` is either implemented end to end or rejected by configuration.

A test that currently records one of these behaviors must be deliberately reclassified or replaced when its owning phase implements the approved break. It must not simply be deleted to obtain a green suite.

## Out-of-scope product features

- merged cross-upstream MCP tool aggregation;
- tool renaming or schema adaptation;
- arbitrary modern-to-legacy protocol translation;
- adoption of unrelated MCP extensions solely because they exist;
- cross-principal sharing of policy-filtered capability caches;
- converting `/summary` into an aggregated MCP server.

## Phase 0 acceptance mapping

| Issue #3 acceptance criterion | Phase 0 evidence |
|---|---|
| Current architecture documented from code | ADR current-architecture sections cite exact source modules/symbol behavior and frozen base SHA. |
| Canonical modern endpoint semantics approved | ADR Decision §2 and target contract above. |
| All supported client/upstream combinations have named test scenarios | All repository-known active upstreams and all repository-supported protocol client classes have stable scenario names/tests. Concrete deployment client products remain explicitly `UNVERIFIED` because the repository contains no such inventory. |
| Legacy bridge consumers identified or provisionally unused | `LEGACY_SSE_BRIDGE_CONSUMERS=PROVISIONALLY_UNUSED`. |
| Out-of-scope product features documented | ADR and this matrix both enumerate non-goals. |
| Baseline tests detect compatibility regressions | New deterministic modern/sessionful/SSE integration baselines plus mapped existing regressions. |
| ADR rejects monolithic merged-tool server | ADR Rejected alternative section. |

## References

Repository authority:

- Issues #1, #3, #5, #6, #7, and #8.
- `mcp_router/server.py`
- `mcp_router/core/config_loader.py`
- `mcp_router/core/process_manager.py`
- `mcp_router/config.yaml`
- `tests/test_router.py`

External protocol authority:

- MCP `2026-07-28` release: <https://blog.modelcontextprotocol.io/posts/2026-07-28/>
