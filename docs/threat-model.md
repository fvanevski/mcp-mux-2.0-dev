# Threat model

## Scope

`mcp-mux` is a local-first HTTP gateway that selects one configured MCP upstream per namespace. It is not an authorization server, secret store, merged-tool server, or protocol translation service. The primary trust boundaries are: caller -> mux, mux -> configured upstream, mux -> managed child process, and configuration/environment -> runtime.

## Protected assets

- caller identity and credentials;
- configured upstream credentials and environment-expanded secrets;
- capability policy decisions;
- request/response payload confidentiality;
- managed-process lifecycle and host resources;
- protocol/routing integrity;
- operational evidence used for review and release decisions.

## Principal threats and controls

| Threat | Control |
|---|---|
| Caller credentials leak upstream | Caller `Authorization`, cookies, proxy authorization, and forwarded identity are stripped; configured upstream headers are applied separately. |
| Forged identity/proxy headers | Forwarded identity headers are removed unless the authenticated trusted-proxy boundary establishes identity. |
| Capability bypass by direct call | Policy authorizes direct named operations before upstream dispatch; denied calls are counted. |
| Forged modern routing headers | Modern `2026-07-28` body metadata and `MCP-Protocol-Version` / `Mcp-Method` / `Mcp-Name` agreement are validated before dispatch. |
| Host/Origin exposure | Local-only mode requires loopback callers; non-loopback binding requires authenticated mode; allowed Host/Origin values are enforced. |
| Secret leakage in logs | Structured request logs contain operational metadata only, never raw request/response bodies or headers. Existing process/upstream error logging passes through the configured redactor. |
| Trace metadata abuse | Only syntactically valid W3C version-00 `traceparent` is forwarded with bounded `tracestate`; invalid trace context and arbitrary caller `baggage` are dropped. Trace values are not logged. |
| Unbounded stream/session work | Runtime leases, bridge queue/session limits, TTL/backpressure policy, cancellation cleanup, and endpoint retirement bound work. |
| Managed child escape/leak | Structured `argv` is preferred, shell execution is explicitly marked unsafe, child process groups are terminated on cleanup, and restart attempts are bounded. |
| Reload races | Runtime-affecting configuration changes drain old runtimes before atomic publication of the new snapshot. |
| Malformed/failing upstream | An upstream response labeled `application/json` must parse as JSON or the gateway returns a generated 502 without forwarding the malformed body; HTTP/transport failures remain observable through bounded status/error handling and counters. |
| Stale validation evidence | Repository assessment binds exact base/head SHAs; PR/gate decisions remain separate from host evidence. |

## Residual risk

- Legacy HTTP+SSE and the local legacy SSE bridge remain compatibility surfaces and therefore retain additional state/lifecycle complexity. They are deprecated, opt-in where applicable, bounded, and observable.
- `unsafe_shell_command` intentionally permits shell semantics for explicitly configured managed endpoints. Treat configuration write access as privileged code-execution authority.
- The gateway cannot make an untrusted upstream safe. Upstream responses are subject to header sanitization, redaction/projection where applicable, but upstream application semantics remain outside mux authority.
- In-process metrics reset on process restart; they are operational counters, not durable audit records.

## Security regression authority

Security regressions live primarily in `tests/test_phase3_security.py`, protocol/routing regressions in `tests/test_phase2_protocol_edge.py`, observability/trace regressions in `tests/test_observability.py`, and lifecycle/compatibility regressions in the Phase 4/5 and compatibility suites.
