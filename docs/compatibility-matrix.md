# MCP compatibility baseline and claim-to-test matrix

- **Phase:** Issue #3 — compatibility contract and architecture baseline
- **Frozen implementation base:** `0d630e7aab347657c7aca108451a0a6850793edb`
- **Architecture decision:** `docs/architecture/ADR-001-endpoint-gateway.md`
- **Modern protocol authority:** MCP `2026-07-28`

## Purpose

This document separates:

1. **current behavior** frozen from the Phase 0 implementation base;
2. **target MCP `2026-07-28` behavior** that later protocol work must implement;
3. **legacy compatibility paths** that remain explicit support surfaces; and
4. **operator-verified client deployments** that define the concrete client scenarios Issue #3 must preserve.

The tests below are falsification points. Later phases may intentionally change a frozen behavior only when the governing issue authorizes the change and the matrix/test classification is updated with it.

## Evidence boundary and concrete client inventory

Issue #3 requires the real client/upstream combinations that must remain supported to be identified. Repository source alone does not contain client application configuration, so Phase 0 combines repository authority with the operator-verified deployment inventory recorded on Issue #3 on 2026-08-29.

`REAL_CLIENT_INVENTORY=OPERATOR_VERIFIED`

### Required concrete modern scenarios

| Scenario ID | Concrete client | Mux namespace | Upstream identity / boundary | Evidence status | Regression |
|---|---|---|---|---|---|
| `opencode-modern-firecrawl` | OpenCode | `/firecrawl` | repository-configured managed Firecrawl endpoint (`http://localhost:3033/mcp`) | operator client config + repository config | `test_operator_verified_modern_client_scenarios[opencode-modern-firecrawl]` |
| `opencode-modern-context7` | OpenCode | `/context7` | repository-configured Context7 endpoint (`https://mcp.context7.com/mcp`) | operator client config + repository config | `test_operator_verified_modern_client_scenarios[opencode-modern-context7]` |
| `opencode-modern-huggingface` | OpenCode | `/huggingface` | repository-configured Hugging Face endpoint (`https://huggingface.co/mcp`) | operator client config + repository config | `test_operator_verified_modern_client_scenarios[opencode-modern-huggingface]` |
| `opencode-modern-github` | OpenCode | `/github` | operator deployment GitHub MCP boundary; not present in the frozen branch config | operator client + deployment config | `test_operator_verified_modern_client_scenarios[opencode-modern-github]` |
| `codex-modern-governed-gh-cli` | Codex | operator-specific governed `gh_cli` namespace | mux-managed governed `gh_mcp`/GitHub CLI service | operator deployment evidence | `test_operator_verified_modern_client_scenarios[codex-modern-governed-gh-cli]` |

These named regressions exercise the protocol invariant common to the concrete deployments: a client namespace selects one Streamable HTTP upstream boundary and valid modern traffic passes through without mux-local protocol-session state. Deployment-specific credentials and private route suffixes are deliberately not copied into repository fixtures.

OpenCode configuration also contains a `/postgres` route in operator files, but the exact backend mapping was not established by the Phase 0 evidence set. Issue #3 therefore explicitly records it as **configured but not a required compatibility commitment**. If it becomes a required deployment commitment, the governing compatibility authority must record its backend identity and add a named scenario before relying on it as preserved behavior.

### Repository-declared active upstream identities

The frozen branch `mcp_router/config.yaml` independently establishes four active upstream identities:

| Scenario ID | Namespace | Mode | Configured upstream | Inferred transport | Regression |
|---|---|---|---|---|---|
| `modern-remote-web-search` | `web-search` | `remote` | `https://mcp.garion.us/mcp` | `streamable-http` | `test_configured_upstreams_are_frozen_as_streamable_http[modern-remote-web-search]` |
| `modern-managed-firecrawl` | `firecrawl` | `managed_cli` | `http://localhost:3033/mcp` | `streamable-http` | `test_configured_upstreams_are_frozen_as_streamable_http[modern-managed-firecrawl]` |
| `modern-remote-huggingface` | `huggingface` | `remote` | `https://huggingface.co/mcp` | `streamable-http` | `test_configured_upstreams_are_frozen_as_streamable_http[modern-remote-huggingface]` |
| `modern-remote-context7` | `context7` | `remote` | `https://mcp.context7.com/mcp` | `streamable-http` | `test_configured_upstreams_are_frozen_as_streamable_http[modern-remote-context7]` |

`web-search` is a repository-known upstream but no concrete downstream client requirement was established. The commented Crawl4AI example is not active configuration and is not a support commitment.

## Legacy bridge consumer inventory

`LEGACY_SSE_BRIDGE_CONSUMERS=PROVISIONALLY_UNUSED`

Evidence:

- `legacy_sse_bridge` is absent/disabled by default and requires an explicit bounded mapping when enabled;
- no active frozen-branch endpoint enables it;
- no operator-verified concrete client is recorded as requiring it;
- current coverage exercises it only as a compatibility adapter.

This is a bounded statement about the available evidence, not proof that no unknown external deployment exists. Phase 5 adds `/summary` utilization/failure counters so any later removal decision can use deployment evidence rather than assumption.

### Phase 5 compatibility resolution

Issue #8 keeps the mux-local SSE adapter as a **deprecated, explicit compatibility surface** rather than part of canonical Streamable HTTP. The adapter is lazy-loaded only for configured bridge traffic; ordinary modern and transparent legacy Streamable HTTP do not instantiate bridge state. Per-endpoint configuration bounds queue capacity, backpressure wait, session TTL, and maximum sessions. Disconnect, expiry, endpoint retirement, backpressure failure, and shutdown close adapter-owned tasks/upstream streams and remove session state. Synchronous upstream setup/rejection failures return meaningful HTTP errors; asynchronous response failures terminate the downstream SSE stream with an error event. `/summary` exposes active-session, use, rejection, expiry, upstream-failure, and backpressure counters.

The stdio decision is also fail-closed: current endpoint configuration accepts only `remote` and `managed_cli`; `stdio_bridge` is rejected until a complete end-to-end adapter exists. This avoids an accepted schema mode with no production implementation or an undocumented dependency on an external HTTP bridge.

## Canonical target contract for MCP `2026-07-28`

The target contract is normative for later implementation, but Phase 0 does not make the current production router enforce it yet.

| Claim | Target contract |
|---|---|
| Public route | One canonical Streamable HTTP endpoint per namespace: `POST /<namespace>`. |
| Session model | Stateless: every request is independently processable and does not depend on a prior mux protocol session. |
| Per-request metadata | Every request carries `params._meta["io.modelcontextprotocol/protocolVersion"]` and `params._meta["io.modelcontextprotocol/clientCapabilities"]`; `clientInfo` is optional. |
| Protocol header | `MCP-Protocol-Version` is required for modern POST and must match the body protocol version. |
| Routing headers | `Mcp-Method` mirrors the JSON-RPC method. `Mcp-Name` mirrors `params.name` for `tools/call`/`prompts/get` and `params.uri` for `resources/read`. |
| Result shape | Successful modern responses include `result.resultType`; `"complete"` represents a completed result. |
| Message shape | Valid JSON-RPC object only; no Streamable HTTP batches or request-body repair. |
| Transparency | Unknown valid fields, extension methods, and `_meta` data survive proxying. |
| Legacy session header | `Mcp-Session-Id` is not modern protocol state. A modern request must not depend on it. |
| Subpaths | Arbitrary `/<namespace>/<subpath>` forwarding is not part of the modern contract; `/<namespace>/` is also non-canonical and must not bypass the subpath restriction. |
| Aggregation | A namespace still selects one upstream; there is no merged-tool MCP server. |

Primary protocol references:

- <https://modelcontextprotocol.io/specification/2026-07-28/basic>
- <https://modelcontextprotocol.io/specification/2026-07-28/basic/transports/streamable-http>
- <https://modelcontextprotocol.io/specification/2026-07-28/server/discover>
- <https://modelcontextprotocol.io/specification/2026-07-28/server/tools>

## Claim-to-test matrix

### A. Modern stateless Streamable HTTP

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `M-ST-01` | `POST /<namespace>` maps to one configured upstream MCP endpoint. | Current + target | all `test_operator_verified_modern_client_scenarios[...]` cases |
| `M-ST-02` | Valid `2026-07-28` per-request metadata and routing headers pass through. | Current pass-through; future strict validation | same |
| `M-ST-03` | Modern requests complete without mux-local bridge sessions. | Current + target | same |
| `M-ST-04` | Frozen branch upstream identity/mode/transport remains explicit. | Current architecture | `test_configured_upstreams_are_frozen_as_streamable_http[...]` |
| `M-ST-05` | Current router normalizes POST `Accept` upstream to `application/json, text/event-stream`. | Current behavior | operator scenario test + existing router regression |
| `M-ST-06` | The deterministic oracle rejects missing required per-request metadata. | Fixture authority | `test_modern_fixture_rejects_missing_required_per_request_metadata[...]` |
| `M-ST-07` | Protocol-version header/body mismatch takes precedence and is a `400` `HeaderMismatch` (`-32020`). | Fixture authority / target | `test_modern_fixture_rejects_protocol_header_body_mismatch` |
| `M-ST-07A` | A matching header/body version that the fixture does not support is `UnsupportedProtocolVersion` (`-32022`) with supported-version data. | Fixture authority / target | `test_modern_fixture_rejects_matching_unsupported_protocol_version` |
| `M-ST-08` | `resources/read` derives `Mcp-Name` from `params.uri`. | Fixture authority / target | `test_modern_fixture_uses_resource_uri_for_mcp_name` |
| `M-ST-09` | A legacy session header is not used as modern protocol state. | Fixture authority / target | `test_modern_fixture_does_not_use_legacy_session_header_for_protocol_state` |
| `M-ST-10` | Successful fixture responses include `resultType`; listed tools include `inputSchema`. | Fixture authority | operator scenario assertions |
| `M-ST-11` | Modern Streamable HTTP accepts only the canonical bare namespace route; both non-empty subpaths and the empty trailing-slash subpath are rejected locally before upstream dispatch. | Phase 2 target | `test_modern_subpath_is_rejected` |
| `M-ST-12` | Long-lived modern event-stream responses are not terminated solely because no response chunk arrives within `upstream_timeout`; request establishment remains bounded and downstream cancellation closes upstream. | Phase 2 target | `test_downstream_cancellation_closes_upstream_response_context` |

The fixture remains the deterministic oracle for a correct modern peer. Phase 2 adds the production gateway validation that rejects malformed or mismatched modern traffic before forwarding, while the Phase 0 fixture continues to provide independent upstream-side compatibility evidence.

### B. Legacy sessionful Streamable HTTP

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `L-SH-01` | Legacy `initialize` can be forwarded. | Current compatibility | `test_legacy_sessionful_streamable_http_baseline` |
| `L-SH-02` | Upstream `Mcp-Session-Id` is returned downstream. | Current compatibility | same |
| `L-SH-03` | Later downstream session header is forwarded transparently without a mux-local bridge session. | Current compatibility | same |
| `L-SH-04` | Mux-owned bridge sessions remain endpoint-scoped. | Preservation invariant | existing `test_streamable_http_rejects_session_for_different_endpoint` |

### C. Legacy HTTP+SSE

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `L-SSE-01` | Explicit `transport="sse"` proxies downstream SSE GET to the legacy upstream path. | Current compatibility | `test_legacy_http_sse_baseline` |
| `L-SSE-02` | Upstream endpoint events are rewritten beneath the mux namespace. | Current compatibility | same |
| `L-SSE-03` | Downstream POST to the rewritten subpath maps to the upstream legacy message path/query. | Current compatibility | same + existing `test_sse_message_post_uses_upstream_message_path` |
| `L-SSE-04` | Arbitrary subpath rewriting is legacy-only in the target architecture. | Target | `test_modern_subpath_is_rejected` plus legacy HTTP+SSE baseline |
| `L-SSE-05` | A valid explicit legacy SSE stream may remain quiet longer than `upstream_timeout`; the timeout bounds request establishment but is not an SSE read-idle limit. | Phase 2 preservation | `test_legacy_sse_stream_has_no_read_idle_timeout` |

### D. Mux-local `legacy_sse_bridge`

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `BR-01` | Bridge is opt-in and lazy; ordinary modern/transparent Streamable HTTP does not instantiate local bridge state. | Current Phase 5 | `test_modern_streamable_http_never_instantiates_bridge_state` + compatibility-baseline stateless assertions |
| `BR-02` | Enabling the bridge requires an explicit bounded mapping and creates an endpoint-owned local session flow. | Current compatibility | `test_legacy_bridge_requires_explicit_bounded_mapping` + `test_streamable_http_sse_get_opens_local_sse_bridge_when_enabled` |
| `BR-03` | Bridge captures/reuses upstream session identity for bridge POSTs. | Current compatibility | `test_streamable_http_direct_response_json` |
| `BR-04` | Cross-endpoint bridge-session reuse is rejected. | Preservation invariant | `test_bridge_session_cannot_cross_endpoint_boundary` + `test_streamable_http_rejects_session_for_different_endpoint` |
| `BR-05` | Removing/changing an endpoint drops its bridge sessions while unchanged endpoints retain theirs. | Current reload invariant | `test_apply_configuration_drops_sessions_for_removed_or_changed_endpoint` + Phase 4 retirement regressions |
| `BR-06` | Queues, backpressure, TTL, and session admission are bounded and deterministic. | Current Phase 5 | `test_bridge_queue_is_bounded_and_backpressure_fails_closed`, `test_bridge_ttl_expires_session_and_rejects_replay`, `test_bridge_session_limit_rejects_excess_admission` |
| `BR-07` | Disconnect/retirement owns cancellation and upstream cleanup; failures are client-observable. | Current Phase 5 | `test_bridge_disconnect_cancels_response_and_closes_upstream`, `test_upstream_http_failure_is_synchronous_and_observable`, `test_async_upstream_failure_reaches_downstream_error_event` |
| `BR-08` | Bridge utilization/failure counters are exposed without activating the bridge for normal traffic. | Current Phase 5 | Phase 5 `/summary` assertions + modern-path lazy-adapter regression |

### E. Protocol validation and response projection

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `CUR-01` | The Phase 2 edge rejects a request object missing `jsonrpc: "2.0"`; it does not repair or forward it. | Intended v0.2.0 breaking change | `test_streamable_http_direct_post_rejects_missing_jsonrpc_version` + Phase 2 protocol-edge regressions |
| `CUR-02` | The Phase 2 edge rejects JSON-RPC batch bodies instead of repairing or forwarding batch items. | Intended v0.2.0 breaking change | `test_streamable_http_direct_post_rejects_jsonrpc_batch` + Phase 2 protocol-edge regressions |
| `CUR-02A` | Strict JSON parsing rejects the non-standard numeric constants `NaN`, `Infinity`, and `-Infinity`; invalid raw bytes are never forwarded upstream. | Phase 2 target | `test_parse_rejects_non_json_numeric_constants` + `test_invalid_streamable_requests_are_rejected_before_upstream` |
| `CUR-03` | Current tool policy projects `tools/list` with allow/deny lists. | Current behavior | existing filter tests |
| `CUR-04` | Tool-list projection does not prove direct-call authorization. | Known security defect | Phase 3 |
| `CUR-05` | Rebuilt JSON responses strip stale encoding/length headers. | Current response-safety behavior | existing regression |

### F. Managed and remote endpoint behavior

| Claim ID | Baseline claim | Classification | Test evidence |
|---|---|---|---|
| `RUN-01` | Remote endpoints proxy without local process startup. | Current architecture | compatibility tests |
| `RUN-02` | `managed_cli` is distinct and frozen Firecrawl remains managed. | Current architecture | configured-upstream regression |
| `RUN-03` | Managed startup has production lifecycle coverage, including a real disposable managed subprocess. | Current behavior | `test_process_manager_lifecycle` + `test_cleanup_leaves_no_real_managed_subprocess_alive` |
| `RUN-04` | Runtime reload/idle behavior is transactional/lease-based. | Current Phase 4 | Phase 4 runtime regressions |
| `RUN-05` | Every accepted endpoint mode is implemented: `remote` uses the proxy path and `managed_cli` adds supervisor-owned process lifecycle. | Current Phase 5 | modern remote compatibility scenarios + managed runtime/process regressions |
| `RUN-06` | `stdio_bridge` is not an accepted endpoint mode and fails configuration validation. | Current Phase 5 | `test_unimplemented_or_unknown_modes_are_rejected[stdio_bridge]` |

## Deterministic mock upstream authority

`tests/fixtures/mock_upstream.py` provides three in-process `httpx.MockTransport` modes:

| Mode | Contract |
|---|---|
| `modern-stateless` | Validates required `2026-07-28` per-request metadata, protocol/method/name header agreement, correct `resources/read` URI routing, and modern result/tool shapes; it does not use legacy session headers as modern state. |
| `legacy-sessionful` | Returns a deterministic session ID from `initialize` and requires it on later legacy requests. |
| `legacy-http-sse` | Emits a deterministic endpoint event and accepts the corresponding legacy message POST. |

These fixtures make no live network calls and are compatibility oracles, not substitutes for the official MCP conformance suite planned for Phase 6.

## Preservation invariants

```text
PRESERVATION_INVARIANTS:
- One configured namespace selects exactly one upstream endpoint/runtime boundary.
- Valid modern MCP v2 traffic remains stateless through the mux.
- Required real client deployments retain named compatibility scenarios.
- Approved legacy sessionful Streamable HTTP remains transparent until explicitly deprecated.
- Legacy HTTP+SSE subpath rewriting exists only on an explicit legacy transport path.
- The mux-local legacy SSE bridge remains opt-in and endpoint/session isolated.
- Upstream tools retain native names/schemas; the mux does not become a merged-tool server.
- Remote and managed endpoint identities remain distinguishable.
- Deterministic compatibility tests do not depend on external service availability.
- Known current defects remain labeled as defects rather than target guarantees.
```

## Intended v0.2.0 breaking changes

The v0.2.0 phases intentionally:

- reject malformed JSON-RPC and Streamable HTTP batches rather than repairing them;
- enforce the modern per-request metadata and routing-header contract;
- remove arbitrary modern namespace subpaths;
- enforce direct capability policy before forwarding;
- isolate caller and upstream credentials;
- harden Host/Origin/caller authentication;
- replace managed-process/reload heuristics with explicit runtime state;
- bound and observe the local legacy adapter;
- reject `stdio_bridge` until a complete end-to-end adapter is implemented.

Tests that encode a behavior being intentionally broken must be reclassified alongside the governing issue, never simply deleted to obtain green status.

## Out-of-scope product features

- merged cross-upstream tool aggregation;
- tool renaming/schema adaptation;
- arbitrary modern-to-legacy translation;
- unrelated MCP features solely because the revision supports them;
- cross-principal sharing of policy-filtered caches;
- converting `/summary` into an aggregated MCP server.

## Phase 0 acceptance mapping

| Issue #3 acceptance criterion | Evidence |
|---|---|
| Current architecture documented from code | ADR source-derived current-architecture sections. |
| Canonical modern endpoint semantics approved | ADR Decision §2 plus the normative target table above. |
| All supported client/upstream combinations have named scenarios | Operator-verified inventory recorded on Issue #3 and parameterized named regressions above. |
| Legacy bridge consumers identified or provisionally unused | `LEGACY_SSE_BRIDGE_CONSUMERS=PROVISIONALLY_UNUSED`. |
| Out-of-scope product features documented | ADR + this matrix. |
| Baseline detects accidental compatibility regressions | Correct modern oracle with negative oracle tests plus legacy integration baselines and existing regressions. |
| ADR rejects monolithic merged-tool server | ADR rejected-alternative section. |

## Repository references

- Issues #1, #3, #5, #6, #7, #8, and #9
- `mcp_router/server.py`
- `mcp_router/core/config_loader.py`
- `mcp_router/core/process_manager.py`
- `mcp_router/config.yaml`
- `tests/test_router.py`
- `tests/test_compatibility_baseline.py`
- `tests/fixtures/mock_upstream.py`
