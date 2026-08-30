# Product Requirements Document: MCP Mux v0.2.0 Refactor

## 1. Document control

| Field                        | Value                                                                                                             |
| ---------------------------- | ----------------------------------------------------------------------------------------------------------------- |
| Product                      | `mcp-mux`                                                                                                         |
| Repository                   | `fvanevski/mcp-mux-2.0-dev`                                                                                       |
| Target release               | `v0.2.0`                                                                                                          |
| Document status              | Implementation PRD; planning/reference authority subject to merged repository contracts                           |
| Primary audience             | Coding AI agent, code reviewer, repository maintainer                                                             |
| Parent epic                  | GitHub issue #1                                                                                                   |
| Release tracker              | GitHub issue #2                                                                                                   |
| Phase issues                 | #3–#9                                                                                                             |
| Quality gates                | #10–#13                                                                                                           |
| Current default branch       | `main`                                                                                                            |
| Architectural style          | Endpoint-per-upstream ASGI gateway and managed-process orchestrator                                               |
| Last synchronized implementation | `main@bb369027a1ae175e4d9e51ea500c060a07a0bfc1` after merged Phase 2 / Issue #5                               |

## 2. Executive summary

`mcp-mux` currently exposes multiple MCP servers through distinct path-prefixed endpoints, including remote MCP services and locally managed HTTP MCP processes. Its central product concept is sound: one lightweight gateway provides stable namespaces such as `/firecrawl`, `/huggingface`, and `/context7`, injects endpoint-specific upstream credentials, manages selected local servers on demand, and supports both Streamable HTTP and legacy HTTP+SSE behavior.

The implementation must now be refactored from a hand-written, partially protocol-translating reverse proxy into a secure, protocol-aware MCP gateway. The gateway must support the MCP `2026-07-28` stateless protocol model while retaining explicitly approved legacy compatibility. It must continue to operate as an ASGI proxy; it must not become a monolithic MCP server that imports and republishes every upstream tool. This architectural direction and the staged issue hierarchy are established in epic #1.

At the refactor baseline, the highest-priority defects were:

1. Tool allowlists affect `tools/list` output but do not prevent direct invocation of hidden tools.
2. The gateway injects privileged upstream credentials without a complete caller-authentication and request-origin boundary.
3. Invalid JSON-RPC requests are repaired instead of rejected.
4. The legacy SSE bridge can leave detached tasks writing to unbounded queues.
5. Managed-process idleness and active work are inferred from incomplete connection counters.
6. Configuration reload can race process shutdown and port reuse.
7. `stdio_bridge` is accepted by configuration but is not implemented end to end.
8. Packaging retains obsolete or unused MCP/FastMCP-era dependencies and modules.

Some baseline defects have already been closed by merged phases. Current phase/gate issues and merged contracts identified in Section 4 are authoritative for remaining work and completion state.

The refactor proceeds through seven implementation phases and four closure gates. Each phase lands through a narrowly scoped pull request or small related PR series. Gate closure requires executable evidence, not merely code-review approval.

---

# 3. Product purpose

## 3.1 Product mission

Provide one locally deployable gateway through which MCP clients can access multiple independent MCP servers at stable, isolated HTTP endpoints, with:

- protocol-correct request forwarding;
- upstream credential isolation;
- per-endpoint capability policy;
- on-demand lifecycle management for local MCP servers;
- explicit and bounded legacy compatibility;
- low operational overhead;
- verifiable security and conformance behavior.

## 3.2 Primary users

### Local operator

A developer or system administrator who configures remote and locally managed MCP servers, upstream tokens, endpoint policy, process timeouts, and gateway exposure.

The operator expects:

- safe localhost defaults;
- deterministic startup and shutdown;
- actionable diagnostics;
- configuration validation before state changes;
- no secret leakage;
- no orphaned subprocesses;
- predictable endpoint behavior after hot reload.

### MCP client

An IDE, coding agent, desktop application, CLI agent, or other MCP host that connects to one configured namespace.

The client expects:

- valid MCP transport semantics;
- accurate tool/resource/prompt discovery;
- no advertised capability that cannot be used;
- no hidden capability that remains directly callable;
- correct JSON-RPC errors;
- preserved SSE framing;
- explicit compatibility behavior.

### Upstream MCP server

A remote service or managed local process that receives requests from the mux.

The upstream expects:

- protocol messages to remain semantically unchanged unless a documented policy transformation applies;
- required headers to be forwarded correctly;
- client credentials not to be confused with upstream credentials;
- request cancellation to propagate;
- sessions to be preserved only for legacy protocol revisions that use them.

### Repository maintainer and coding agent

The implementation agent must be able to work one phase at a time, map every change to a requirement and GitHub issue, run deterministic validation, and avoid architectural drift.

---

# 4. Source-of-truth hierarchy

When sources conflict, use this order:

1. **Applicable final MCP specification and official SDK documentation** for protocol requirements.
2. **Current governing GitHub phase/gate issues and merged repository contracts** (`docs/architecture/ADR-001-endpoint-gateway.md`, `docs/compatibility-matrix.md`, `docs/validation.md`, and `docs/configuration.md`) for implemented scope, closure evidence, compatibility, and validation authority.
3. **This PRD** for product architecture, remaining scope, security invariants, and sequencing where it does not conflict with a more current merged contract or issue decision.
4. **Tests** for accepted implemented behavior.
5. **Existing implementation and README** as evidence of current behavior, with merged implementation taking precedence over historical planning prose.

This document originated before several phases were implemented. Historical current-state descriptions, suggested branch names, CI targets, and coding-agent operating instructions below are planning context unless reaffirmed by a current governing issue or merged repository contract. In particular, `docs/validation.md` governs the current Central/local-agent validation workflow.

The implementation agent must not preserve existing behavior merely because a current test asserts it. Tests that encode intentional protocol repair, insecure behavior, or invalid semantics must be replaced as part of the phase that changes the governing requirement.

## 4.1 External SDK-version checkpoint

**RESOLVED in Phase 1 / Gate A.** The repository now declares `mcp>=2,<3`, and the authoritative lockfile resolves `mcp==2.1.1`. Issue #4 and Gate A (#10) were closed with exact-head CI, deterministic host assessment, and merged-main evidence.

Future dependency work must treat the committed `pyproject.toml`, `uv.lock`, governing issue, and current validation contract as authority rather than re-running this historical checkpoint unless a later issue explicitly requires an SDK transition.

This resolved checkpoint preserves the original Phase 1 intent while preventing stale prerelease guidance from overriding the merged dependency state.

---

# 5. Baseline architecture at refactor start (historical)

This section describes the pre-refactor baseline that motivated the phase plan. It is retained for provenance, not as a description of the current merged implementation. For current behavior, use the merged source plus the repository contracts identified in Section 4.

## 5.1 Runtime topology

The baseline runtime was:

```text
main.py
└── Uvicorn
    └── Starlette application
        └── MCPRouter
            ├── GET /summary
            ├── /{path_prefix}
            └── /{path_prefix}/{subpath...}
                ├── remote Streamable HTTP
                ├── remote legacy HTTP+SSE
                ├── managed local HTTP MCP process
                └── optional local SSE compatibility bridge
```

`main.py` defaults to `127.0.0.1`, which is a correct local-deployment default.

`MCPRouter` currently combines route registration, URL rewriting, transport classification, response transformation, bridge-session management, process activation, activity accounting, and configuration application in one class.

Configuration is loaded from YAML, environment variables are expanded recursively, and a polling watcher applies changes at runtime.

Managed CLI endpoints are launched through a shell command, declared ready when their TCP port opens, and terminated through Unix process-group signals.

## 5.2 Current endpoint model

The production example includes:

- remote Web Search;
- managed Firecrawl;
- remote Hugging Face;
- remote Context7.

Each is exposed under a separate path namespace.

This namespace isolation is a retained product requirement.

## 5.3 Current policy behavior

The gateway rewrites a `tools/list` result to remove tools outside an allowlist or inside a denylist. It does not inspect inbound `tools/call` requests for the requested tool name.

The existing tests verify list projection but do not establish direct-call authorization.

## 5.4 Current protocol repair

The gateway currently inserts a missing `"jsonrpc": "2.0"` member and performs this operation on JSON arrays. Existing tests require this behavior.

This behavior must be removed. Valid MCP Streamable HTTP POST bodies contain one JSON-RPC message, and a gateway that parses the body must reject malformed or mismatched messages rather than silently repairing them. The current draft transport specification also requires a single MCP endpoint, standard request headers, body/header consistency, Origin validation, and one JSON-RPC message per POST.

---

# 6. Product goals

## G-1: Preserve endpoint isolation

Every configured upstream remains reachable through its own namespace:

```text
/<namespace>
```

The mux must not merge independent upstream tool registries into one shared MCP server as part of this refactor.

## G-2: Support modern stateless MCP traffic

For protocol version `2026-07-28`, the gateway must support sessionless request forwarding and must not manufacture local protocol sessions for ordinary traffic.

The new revision removes protocol-level sessions and requires standardized routing headers for Streamable HTTP requests.

## G-3: Retain approved legacy interoperability

Legacy initialize/session behavior and deprecated HTTP+SSE compatibility may remain only where identified in the Phase 0 compatibility matrix.

Compatibility behavior must be:

- explicit;
- isolated from the normal modern path;
- bounded;
- observable;
- removable without redesigning the core proxy.

## G-4: Establish a real security boundary

The mux must safely hold and inject upstream credentials. A caller must not gain access to those credentials or their authority merely by reaching the gateway.

## G-5: Enforce capability policy consistently

The same policy source must control both:

- what a client sees during discovery; and
- what the client may invoke directly.

## G-6: Make runtime lifecycle deterministic

A managed process must not:

- start twice;
- be replaced before its old instance releases the port;
- be terminated while upstream work is active;
- survive gateway shutdown;
- enter an unbounded restart loop.

## G-7: Provide auditable implementation evidence

Every phase must include tests and PR evidence sufficient to close the corresponding gate.

---

# 7. Non-goals

The following are excluded from `v0.2.0`:

1. A unified endpoint that aggregates every upstream tool.
2. Tool renaming or schema rewriting for product ergonomics.
3. General translation between arbitrary MCP revisions.
4. Adding Tasks, MCP Apps, or other extensions without a separate product requirement.
5. Sharing transformed discovery caches across principals.
6. Replacing upstream authentication mechanisms.
7. Building a general-purpose API gateway unrelated to MCP.
8. Adding persistent distributed runtime state.
9. Supporting arbitrary user-supplied upstream URLs through a public API.
10. Maintaining every accidental route accepted by the current catch-all router.

---

# 8. Core architectural invariants

These invariants are mandatory throughout implementation.

## INV-1: One namespace maps to one endpoint runtime

A configured path maps to exactly one validated endpoint configuration and one runtime object.

## INV-2: Modern requests are stateless at the mux

The mux may maintain operational state such as active-request leases, metrics, connection pools, or process state. It must not introduce protocol-level sessions for `2026-07-28` requests.

## INV-3: Header and body sources of truth must agree

When the mux parses a Streamable HTTP body, it must validate required MCP routing headers against that body.

For applicable requests:

- `Mcp-Method` must equal `method`;
- `Mcp-Name` must equal `params.name` or `params.uri`;
- recognized `Mcp-Param-*` headers must match their corresponding arguments.

Header names are case-insensitive; method and capability-name values remain case-sensitive. Mismatches must return HTTP `400`, with the standardized `HeaderMismatch` error where applicable.

## INV-4: No policy decision may rely only on discovery output

Filtering `tools/list` is not authorization.

## INV-5: Caller credentials and upstream credentials are separate

Inbound `Authorization`, cookies, or proxy identity headers must not be forwarded upstream unless an endpoint explicitly and safely permits them.

## INV-6: Every accepted configuration mode works end to end

A mode that is not implemented must be rejected during configuration validation.

## INV-7: Configuration publication is atomic

A candidate configuration is either fully validated and published or rejected without partially changing active runtime state.

## INV-8: Every background task has an owner

Every task, stream, subprocess, queue, and client must be reachable through an owning lifespan or runtime object and must have deterministic cleanup.

## INV-9: Unknown MCP extensions pass transparently

The mux may parse the generic JSON-RPC envelope and fields required for routing or policy. It must not discard unknown request members, `_meta` values, extension methods, or upstream response members.

## INV-10: Secrets never appear in normal logs

Configured tokens, cookies, authorization values, task arguments marked sensitive, and environment secrets must be redacted.

---

# 9. Target architecture

```text
ASGI Application
├── SecurityMiddleware
│   ├── Host validation
│   ├── Origin validation
│   ├── caller authentication
│   └── request-size and concurrency limits
├── EndpointRegistry
│   └── EndpointRuntime[namespace]
├── ProtocolEdge
│   ├── JSON-RPC envelope parser
│   ├── protocol-revision classifier
│   ├── standard-header validator
│   ├── method/name extractor
│   └── JSON-RPC error builder
├── PolicyEngine
│   ├── method authorization
│   ├── tool/resource/prompt authorization
│   └── discovery-result projection
├── ProxyTransport
│   ├── shared httpx.AsyncClient
│   ├── ordinary JSON response forwarding
│   ├── SSE event forwarding
│   └── downstream-cancellation propagation
├── RuntimeSupervisor
│   ├── state machine
│   ├── process manager
│   ├── request leases
│   ├── readiness probes
│   └── transactional reload
├── LegacyCompatibility
│   ├── legacy HTTP+SSE route adapter
│   └── optional SSE-to-Streamable-HTTP bridge
└── Observability
    ├── structured logs
    ├── metrics
    └── trace propagation
```

## 9.1 Recommended module boundaries

Exact filenames may change with documented justification, but responsibilities must remain separated.

```text
mcp_router/
├── app.py
├── config/
│   ├── models.py
│   ├── loader.py
│   └── watcher.py
├── protocol/
│   ├── envelope.py
│   ├── headers.py
│   ├── errors.py
│   └── versions.py
├── policy/
│   ├── engine.py
│   └── models.py
├── proxy/
│   ├── client.py
│   ├── http.py
│   └── sse.py
├── runtime/
│   ├── endpoint.py
│   ├── registry.py
│   ├── leases.py
│   └── process.py
├── compat/
│   ├── legacy_http_sse.py
│   └── legacy_sse_bridge.py
├── security/
│   ├── auth.py
│   ├── origin.py
│   └── headers.py
└── observability/
    ├── logging.py
    ├── metrics.py
    └── tracing.py
```

`server.py` must no longer contain all protocol, policy, transport, runtime, and compatibility behavior.

---

# 10. Functional requirements

## 10.1 Endpoint routing

### ROUTE-001

The canonical modern MCP endpoint is:

```text
POST /<namespace>
```

### ROUTE-002

A modern request must not require a method-specific URL such as:

```text
/<namespace>/tools/list
/<namespace>/initialize
```

### ROUTE-003

`GET /<namespace>` may be forwarded only where valid for the configured transport and protocol behavior.

### ROUTE-004

Arbitrary subpath forwarding is permitted only for an explicitly configured legacy HTTP+SSE endpoint, including the upstream POST endpoint announced by the legacy `endpoint` SSE event.

### ROUTE-005

`GET /summary` remains available as lightweight non-MCP operational discovery.

### ROUTE-006

Reserved names, including `summary`, must not be usable as endpoint namespaces.

### ROUTE-007

Unknown endpoint namespaces return HTTP `404` without starting or contacting any process.

---

## 10.2 JSON-RPC and MCP envelope handling

### PROTO-001

The gateway must accept exactly one JSON-RPC message per Streamable HTTP POST.

### PROTO-002

The gateway must reject:

- invalid JSON;
- JSON arrays used as batch messages;
- missing or invalid `jsonrpc`;
- invalid JSON-RPC message shape;
- bodies exceeding the configured size limit.

### PROTO-003

The gateway must not add, remove, or repair protocol members merely to make an invalid message acceptable.

### PROTO-004

The gateway must extract, at minimum:

```text
jsonrpc
id, if present
method, if present
params.name, if applicable
params.uri, if applicable
params.taskId, if an approved extension later requires it
```

### PROTO-005

The gateway must preserve the original serialized request body when no transformation is required.

Where body validation requires parsing, the gateway should still forward the original bytes after successful validation rather than reserializing, unless a documented transformation is necessary.

### PROTO-006

Unknown fields, `_meta`, extension methods, and response result variants must pass through unchanged.

### PROTO-007

`server/discover` must be forwarded or handled according to the official SDK integration design. It must not be silently translated into legacy `initialize`.

### PROTO-008

Legacy requests without modern routing headers must be accepted only when classified as an approved earlier protocol path.

---

## 10.3 Standard MCP headers

### HDR-001

The gateway must use case-insensitive header-name access.

### HDR-002

For modern Streamable HTTP POSTs, the gateway must require `Mcp-Method`.

### HDR-003

For `tools/call`, `resources/read`, and `prompts/get`, the gateway must require `Mcp-Name`.

### HDR-004

The gateway must reject missing, malformed, or mismatched required routing headers before endpoint policy or upstream dispatch.

### HDR-005

Unrecognized `Mcp-Param-*` headers must be forwarded unless blocked by general header-safety rules.

### HDR-006

Recognized `Mcp-Param-*` headers must be decoded and compared to the corresponding body argument.

### HDR-007

The gateway must preserve `MCP-Protocol-Version` upstream after validating that the configured upstream supports the intended forwarding path.

### HDR-008

Hop-by-hop HTTP headers must not be forwarded.

### HDR-009

When a response body is decoded or modified, stale entity and cache headers must be removed or recomputed.

---

## 10.4 Capability policy

### POLICY-001

Policy evaluation must use:

```text
principal
endpoint namespace
JSON-RPC method
named capability, where applicable
```

### POLICY-002

A tool allowlist means only listed tools may be called.

### POLICY-003

A tool denylist means listed tools may not be called.

### POLICY-004

If both allow and deny policy are supplied, configuration validation must fail. The loader must not silently discard one list.

### POLICY-005

`tools/list` results must be projected through the same policy used for `tools/call`.

### POLICY-006

A denied `tools/call` must be rejected without opening an upstream request.

### POLICY-007

A denied valid JSON-RPC request must receive a valid JSON-RPC error using a centrally defined implementation-specific server error code.

### POLICY-008

Policy infrastructure must be extensible to:

- `resources/list`;
- `resources/read`;
- `prompts/list`;
- `prompts/get`;
- approved extension methods.

### POLICY-009

Policy-filtered discovery results must preserve deterministic ordering from the upstream.

### POLICY-010

Any discovery cache must include at least:

```text
endpoint
principal or authorization scope
policy version/hash
protocol revision
```

as cache-key dimensions.

---

## 10.5 Caller authentication and request exposure

### SEC-001

Default gateway exposure is `local_only`.

### SEC-002

`local_only` mode must:

- default-bind to `127.0.0.1`;
- reject unapproved Host values;
- validate any present Origin;
- disable wildcard credentialed CORS.

The MCP transport specification requires Origin validation, recommends localhost binding for local servers, and recommends authentication.

### SEC-003

Binding to a non-loopback interface must require an explicit non-local security mode.

### SEC-004

The first required non-local mode must support either:

- a direct gateway API key; or
- identity from an explicitly configured trusted reverse proxy.

### SEC-005

Forwarded identity headers must not be trusted unless the immediate peer is configured as trusted.

### SEC-006

Inbound `Authorization`, `Cookie`, `Proxy-Authorization`, and similar credential headers must be stripped before upstream dispatch by default.

### SEC-007

Endpoint-specific upstream credentials must override only the upstream header set, not caller identity.

### SEC-008

No endpoint summary, configuration error, exception, or log line may expose secret values.

### SEC-009

Rate and concurrency limits must be configurable per endpoint, with optional per-capability overrides.

### SEC-010

Security middleware must run before managed-process activation so rejected callers cannot start local servers.

---

## 10.6 Proxy transport

### HTTP-001

Use one lifespan-managed `httpx.AsyncClient` or one controlled pool set, not one new client per request.

### HTTP-002

Connection and timeout policy must distinguish:

- connect timeout;
- request-header timeout;
- non-streaming response timeout;
- streaming idle timeout;
- total startup/readiness timeout.

### HTTP-003

Non-streaming JSON responses should be passed through without reserialization unless policy projection or error normalization requires modification.

### HTTP-004

Response status codes and safe headers must be preserved.

### HTTP-005

Notifications or responses accepted without a response body must use the applicable MCP/HTTP `202` behavior.

### HTTP-006

Upstream connection failure maps to an actionable gateway error and structured log event.

### HTTP-007

Client disconnect must cancel the upstream request or close the upstream stream promptly.

### HTTP-008

The proxy must not buffer an entire SSE response.

---

## 10.7 SSE behavior

### SSE-001

SSE parsing must operate on events, not individual text lines.

### SSE-002

The proxy must preserve:

- comments;
- blank event separators;
- `event`;
- multiline `data`;
- `id`;
- `retry`;
- ordering.

### SSE-003

For SSE responses, return:

```text
Cache-Control: no-cache
X-Accel-Buffering: no
```

where appropriate.

The MCP transport draft recommends `X-Accel-Buffering: no` for SSE responses.

### SSE-004

Any event whose MCP payload is transformed by policy must remain valid SSE and valid JSON-RPC.

### SSE-005

Malformed upstream SSE must fail in a controlled manner and release the upstream connection.

---

## 10.8 Configuration

### CONFIG-001

Configuration must use typed, discriminated endpoint models.

Required supported endpoint kinds:

```text
remote_http
managed_http
legacy_http_sse
```

A future `stdio` kind may be added only with a complete adapter.

### CONFIG-002

Each endpoint must define:

```text
path
kind
summary
transport/upstream definition
policy
```

as applicable.

### CONFIG-003

Managed endpoints must represent commands as structured `argv`.

### CONFIG-004

Managed endpoints may define:

```text
env
cwd
idle_timeout_seconds
startup_timeout_seconds
shutdown_timeout_seconds
readiness
restart policy
```

### CONFIG-005

Shell execution must require an explicit field such as:

```yaml
unsafe_shell_command: "..."
allow_unsafe_shell: true
```

A generic `command` string must not silently imply shell execution.

### CONFIG-006

Endpoint paths must:

- be unique;
- be non-empty;
- contain only the approved route-character set;
- contain no `/`;
- avoid reserved application routes.

### CONFIG-007

Remote upstream URLs must use approved schemes.

### CONFIG-008

Managed upstream URLs must default to loopback and require explicit override for non-loopback targets.

### CONFIG-009

Initial invalid configuration must fail application startup.

### CONFIG-010

Invalid reload must leave the complete last known-good configuration and runtime registry active.

### CONFIG-011

Configuration changes must be diffed structurally so that summary-only or policy-only changes do not unnecessarily restart a process unless restart is required.

### CONFIG-012

The repository must contain one canonical dependency and configuration source.

### CONFIG-013

Environment interpolation must retain current required/default semantics, but secret values must not appear in validation errors.

---

## 10.9 Recommended configuration shape

The exact syntax may be refined in Phase 1, but the concepts below are normative:

```yaml
config_version: 2

server:
  host: 127.0.0.1
  port: 8012

  security:
    mode: local_only
    allowed_hosts:
      - 127.0.0.1
      - localhost
    allowed_origins: []

  limits:
    max_request_body_bytes: 2097152
    max_concurrent_requests: 128

endpoints:
  - path: firecrawl
    kind: managed_http
    summary: Firecrawl Web Content Extraction Tool

    upstream:
      url: http://127.0.0.1:3033/mcp
      transport: streamable_http
      headers: {}

    process:
      argv:
        - npx
        - --yes
        - firecrawl-mcp
      env:
        HTTP_STREAMABLE_SERVER: "true"
        PORT: "3033"
        HOST: "127.0.0.1"
        FIRECRAWL_API_URL: "${FIRECRAWL_API_URL}"
      idle_timeout_seconds: 300
      startup_timeout_seconds: 30
      readiness:
        type: mcp
        legacy_fallback: true

    policy:
      tools:
        allow:
          - firecrawl_search
          - firecrawl_scrape

  - path: huggingface
    kind: remote_http
    summary: Hugging Face MCP Server

    upstream:
      url: https://huggingface.co/mcp
      transport: streamable_http
      headers:
        Authorization: "Bearer ${HF_TOKEN:-}"

    policy:
      tools:
        allow: null
```

The implementation must provide a migration example from the existing configuration.

---

## 10.10 Endpoint runtime

### RUNTIME-001

Every active endpoint must be represented by an `EndpointRuntime`.

Minimum state:

```python
class RuntimeState(Enum):
    STOPPED = "stopped"
    STARTING = "starting"
    RUNNING = "running"
    DRAINING = "draining"
    FAILED = "failed"
```

### RUNTIME-002

Minimum runtime-owned data:

```text
validated config
runtime state
serialization lock
managed process handle
process log tasks
active upstream-work leases
last completed activity
restart state
legacy sessions/tasks, if enabled
```

### RUNTIME-003

Only one coroutine may transition a runtime between lifecycle states at a time.

### RUNTIME-004

A request obtains an upstream-work lease before dispatch and releases it only after:

- response completion;
- stream closure;
- cancellation;
- controlled failure.

### RUNTIME-005

Idle shutdown is permitted only when:

```text
state == RUNNING
active_leases == 0
now - last_completed_activity >= idle_timeout
```

### RUNTIME-006

An open downstream connection that is not associated with upstream work must not continuously refresh process activity.

### RUNTIME-007

A managed process must not be killed while an upstream lease exists.

### RUNTIME-008

Unexpected process exit must transition the runtime to `FAILED` and record the exit status.

### RUNTIME-009

Restart attempts must use bounded exponential backoff or another documented bounded strategy.

### RUNTIME-010

Application shutdown must:

1. stop accepting new work;
2. drain or cancel active requests according to shutdown policy;
3. close compatibility sessions;
4. terminate managed processes;
5. await log-reader tasks;
6. close the shared HTTP client;
7. stop the configuration watcher.

---

## 10.11 Managed process requirements

### PROCESS-001

Prefer `asyncio.create_subprocess_exec()`.

### PROCESS-002

Use `start_new_session=True` for process-group isolation on supported Unix systems.

### PROCESS-003

Shell execution is opt-in and must produce a warning indicating the configured endpoint uses unsafe shell mode.

### PROCESS-004

Stdout and stderr readers belong to the corresponding runtime.

### PROCESS-005

Readiness must include:

1. TCP connectivity where applicable;
2. MCP-level discovery;
3. approved legacy initialize fallback.

### PROCESS-006

TCP port readiness alone is insufficient for `RUNNING`.

### PROCESS-007

Shutdown sequence:

1. mark `DRAINING`;
2. reject or queue new work according to policy;
3. wait for leases up to the drain timeout;
4. send `SIGTERM` to the process group;
5. wait for graceful exit;
6. send `SIGKILL` after timeout;
7. await process and log tasks;
8. transition to `STOPPED` or `FAILED`.

---

## 10.12 Hot reload

### RELOAD-001

Reload processing must be serialized.

### RELOAD-002

The loader must fully parse and validate a candidate configuration before applying any change.

### RELOAD-003

The application must calculate:

```text
added endpoints
removed endpoints
restart-required changes
in-place changes
unchanged endpoints
```

### RELOAD-004

Removed or restart-required endpoints must drain before process termination.

### RELOAD-005

A replacement endpoint using the same port must not start until the previous process has terminated and the port is released.

### RELOAD-006

The new registry snapshot must be published atomically.

### RELOAD-007

Requests that began against an old runtime must retain a valid reference until completion or cancellation.

---

## 10.13 Legacy compatibility

### LEGACY-001

Legacy compatibility must live outside the default modern request path.

### LEGACY-002

`legacy_sse_bridge` is disabled by default.

### LEGACY-003

Every bridge session must own:

```text
endpoint
bounded queue
upstream session identifier, if applicable
response tasks
upstream streams
creation time
last activity
cancellation scope
```

### LEGACY-004

Queues must have a configured finite maximum size.

### LEGACY-005

Queue overflow behavior must be explicit and tested.

### LEGACY-006

Client disconnect must cancel all associated tasks and close all associated upstream responses.

### LEGACY-007

Sessions must have:

- TTL;
- endpoint limit;
- global limit;
- deterministic cleanup.

### LEGACY-008

Cross-endpoint session reuse must remain impossible.

### LEGACY-009

The bridge must return meaningful failure information rather than always returning an opaque `202`.

### LEGACY-010

Bridge utilization must be measurable so future removal is evidence-based.

### LEGACY-011

`stdio_bridge` must either:

- be replaced with a full official SDK v2 stdio client adapter; or
- be removed from accepted configuration and documentation.

---

# 11. Nonfunctional requirements

## 11.1 Correctness

- The gateway must not alter valid MCP semantics except for approved policy projection.
- Header/body validation must happen before routing based on those values.
- Responses must retain the originating JSON-RPC request ID.
- Notifications must not receive ordinary JSON-RPC responses.
- Error behavior must be deterministic and tested.

## 11.2 Security

- Safe local deployment is the default.
- Non-local exposure is explicit.
- Secrets are isolated and redacted.
- Denied capability calls never reach upstream.
- Shell execution is not the default.
- Queue, body, stream, and concurrency resources are bounded.

## 11.3 Reliability

- No orphaned tasks or subprocesses.
- No reload/port race.
- No process termination during active upstream work.
- Cancellation propagates in both ordinary and streaming paths.
- Invalid reload does not degrade the active configuration.

## 11.4 Performance

Initial targets:

- Avoid body reserialization on transparent requests.
- Avoid full SSE buffering.
- Reuse HTTP connections.
- Gateway-added p95 latency for ordinary local non-streaming forwarding should remain below 10 ms in the integration benchmark, excluding upstream execution and cold process startup.
- Memory growth must remain bounded under maximum configured concurrent streams and compatibility sessions.

Performance targets may be adjusted only with benchmark evidence documented in the implementing PR.

## 11.5 Maintainability

- No single module should own protocol parsing, policy, lifecycle, security, and compatibility simultaneously.
- Public interfaces must have type annotations.
- Runtime state transitions must be explicit.
- Project-specific JSON-RPC error codes must be centralized.
- All configuration fields must be documented.

## 11.6 Compatibility

The supported matrix must be finalized in Phase 0. At minimum, tests must cover:

```text
modern client → modern remote upstream
modern client → modern managed upstream
legacy client → supported legacy Streamable HTTP upstream
legacy HTTP+SSE client → legacy upstream
approved legacy SSE client → explicit compatibility bridge
```

---

# 12. Error contract

## 12.1 Gateway HTTP errors

| Condition                                   | HTTP status | Body                                      |
| ------------------------------------------- | ----------: | ----------------------------------------- |
| Unknown namespace                           |         404 | Structured gateway error                  |
| Invalid Host                                |  400 or 403 | No secret detail                          |
| Invalid Origin                              |         403 | Optional JSON-RPC error without ID        |
| Missing caller authentication               |         401 | Authentication challenge where applicable |
| Caller authenticated but endpoint forbidden |         403 | Structured error                          |
| Body too large                              |         413 | Structured gateway error                  |
| Unsupported content type                    |         415 | Structured gateway error                  |
| Invalid or unsupported protocol version     |         400 | Structured MCP error                      |
| Header/body mismatch                        |         400 | JSON-RPC `HeaderMismatch`, code `-32001`  |
| Upstream unavailable                        |  502 or 503 | Structured gateway error                  |
| Upstream timeout                            |         504 | Structured gateway error                  |
| Managed endpoint starting/draining          |         503 | Retry metadata where applicable           |

## 12.2 JSON-RPC errors

Use standard JSON-RPC codes where applicable:

```text
-32700 Parse error
-32600 Invalid Request
-32601 Method not found
-32602 Invalid params
-32603 Internal error
```

Use centrally defined implementation-specific codes in `-32000` through `-32099` for:

- capability denied;
- endpoint unavailable;
- runtime draining;
- policy mismatch;
- other mux-specific errors.

Do not scatter numeric literals through handlers.

---

# 13. Security threat model

## T-1: DNS rebinding

**Threat:** a remote page reaches a local mux through browser-controlled DNS.

**Controls:**

- Origin validation;
- Host validation;
- loopback binding;
- no wildcard credentialed CORS.

## T-2: Credential-bearing confused deputy

**Threat:** an untrusted caller uses the mux’s configured Hugging Face, Context7, GitHub, or other upstream credentials.

**Controls:**

- caller authentication;
- exposure modes;
- endpoint authorization;
- no automatic forwarding of caller authorization;
- upstream credential injection after security checks.

## T-3: Capability-policy bypass

**Threat:** a hidden tool is invoked directly.

**Controls:**

- request-body/header parsing;
- policy check before dispatch;
- no upstream request on denial;
- bypass regression tests.

## T-4: Header/body split-brain

**Threat:** infrastructure routes on `Mcp-Name: safe_tool`, while the body executes `dangerous_tool`.

**Controls:**

- strict equality validation;
- HTTP `400`;
- `HeaderMismatch` error;
- validation before policy or routing.

## T-5: Secret leakage

**Threat:** credentials appear in logs, exception strings, summaries, process command logging, or configuration errors.

**Controls:**

- structured redaction;
- secret-aware configuration models;
- sanitized process logging;
- negative tests that scan captured logs.

## T-6: Shell command injection

**Threat:** endpoint configuration is interpolated into a shell command.

**Controls:**

- structured argv;
- structured environment;
- explicit unsafe shell escape hatch;
- trusted configuration boundary documented.

## T-7: Resource exhaustion

**Threat:** unbounded body, queue, session, stream, task, or process growth.

**Controls:**

- finite request size;
- finite bridge queues;
- concurrency limits;
- session TTL and counts;
- task ownership;
- backpressure and cancellation.

## T-8: Reload race

**Threat:** old and new managed processes contend for the same port or receive traffic simultaneously.

**Controls:**

- serialized reload;
- drain;
- awaited stop;
- atomic registry publication.

---

# 14. Observability requirements

## 14.1 Structured request log

Each completed request should record:

```text
request_id
principal identifier or anonymous scope
endpoint namespace
protocol version
transport class
method
capability name, if applicable
policy decision
HTTP status
JSON-RPC error code, if any
duration
bytes received
bytes sent
streamed boolean
managed process cold-start boolean
```

Arguments must not be logged by default.

## 14.2 Runtime metrics

At minimum:

```text
requests_total
request_duration
upstream_errors_total
policy_denials_total
active_endpoint_leases
active_sse_streams
managed_process_state
managed_process_starts_total
managed_process_restarts_total
managed_process_failures_total
legacy_sessions_active
legacy_session_rejections_total
reload_success_total
reload_failure_total
```

## 14.3 Trace propagation

Where supported, propagate documented W3C Trace Context metadata without allowing arbitrary untrusted trace values to contaminate logs or metrics.

The `2026-07-28` specification documents trace-context propagation and cache metadata to improve gateway routability and observability.

---

# 15. Priority implementation sequence

This section records the original phase sequence and deliverable plan. Use current GitHub issue/gate state for completion status and current branch/PR instructions.

The GitHub issue flow is:

```text
#3 → #4 → #10
          ├──→ #5 → #6 → #11
          └──→ #7 ─┐
               #5 ─┼→ #8 → #12
#11 + #12 + #9 ─────────→ #13 → v0.2.0
```

This dependency structure is established in epic #1.

## Priority slice A: Phase 0 — compatibility baseline

**Issue:** #3
**Required PR:** `docs/compatibility-baseline`

Deliver:

- architecture ADR;
- compatibility matrix;
- deterministic mock upstreams;
- baseline behavioral tests;
- explicit breaking-change list;
- legacy bridge consumer inventory.

Issue #3 defines these deliverables and must land first.

### Agent stop condition

Do not begin broad protocol rewriting until the compatibility matrix and canonical endpoint decision exist.

## Priority slice B: Phase 1 — dependencies and typed configuration

**Issue:** #4
**Required PR:** `refactor/mcp-v2-foundation`

Deliver:

- SDK version checkpoint;
- canonical dependency metadata;
- typed endpoint configuration;
- initial-startup failure on invalid config;
- last-known-good reload behavior;
- removal of nonfunctional or stale modes/modules;
- structured managed commands.

Issue #4 defines the dependency and configuration foundation.

### Agent stop condition

Do not declare the SDK migration complete until the lockfile demonstrably resolves the selected official v2 build.

## Priority slice C: Immediate capability-policy hotfix

**Issue relationship:** first security slice of #6
**Suggested PR:** `security/enforce-tool-call-policy`

This is permitted before full Phase 3 completion because it closes an existing authorization bypass.

Implement:

- generic extraction of `method` and `params.name`;
- allow/deny decision before dispatch;
- direct-call denial test;
- proof that denied calls produce no upstream request.

Constraints:

- Do not attempt the complete modern-header implementation in this hotfix.
- Design the policy interface so Phase 2 can supply its validated parsed envelope later.
- Do not close issue #6 or Gate B based only on this slice.

## Priority slice D: Phase 2 — strict protocol edge

**Issue:** #5
**Required PR:** `refactor/protocol-edge`

Deliver:

- strict JSON-RPC parsing;
- no repair;
- no batch forwarding;
- standard-header validation;
- canonical `/<namespace>` route;
- shared HTTP client;
- event-aware SSE forwarding;
- cancellation propagation;
- modern/legacy classifier.

Gate A must be closed before this PR merges.

## Priority slice E: Phase 3 — security boundary

**Issue:** #6
**Suggested PR series:**

```text
security/origin-host
security/auth-header-isolation
security/policy-engine
security/rate-limits
```

Deliver the complete security model and close Gate B only after protocol and security regression evidence exists.

## Priority slice F: Phase 4 — runtime supervision

**Issue:** #7
**Required PR:** `refactor/endpoint-runtime`

May proceed in parallel with Phase 2 after Phase 1, provided shared interfaces are coordinated.

Deliver:

- runtime state machine;
- lease accounting;
- transactional reload;
- managed readiness;
- structured subprocess execution;
- deterministic process and task cleanup.

## Priority slice G: Phase 5 — legacy isolation and stdio decision

**Issue:** #8
**Required PR:** `refactor/legacy-adapters`

Requires both the protocol edge and runtime primitives.

Deliver:

- isolated compatibility module;
- bounded queues and sessions;
- disconnect cleanup;
- bridge metrics;
- complete stdio adapter or schema removal.

Close Gate C only after repeated lifecycle and leak tests.

## Priority slice H: Phase 6 — conformance and release

**Issue:** #9
**Required PR:** `release/v0.2.0`

Deliver:

- reorganized test suite;
- compatibility integration matrix;
- official conformance evidence;
- CI;
- observability;
- threat model;
- deployment guide;
- migration guide;
- version and release notes.

Close Gate D only after final release checks complete.

---

# 16. Test requirements

## 16.1 Configuration tests

Required scenarios:

1. Valid remote endpoint.
2. Valid managed endpoint with structured argv.
3. Duplicate path rejected.
4. Reserved path rejected.
5. Path containing `/` rejected.
6. Unknown endpoint kind rejected.
7. Unsupported transport rejected.
8. Allow and deny policy together rejected.
9. Missing required environment variable rejected without exposing secrets.
10. Invalid initial configuration prevents startup.
11. Invalid reload retains old runtime.
12. Summary-only change does not restart process.
13. Process-affecting change drains and restarts.
14. Unsupported `stdio_bridge` rejected unless fully implemented.

## 16.2 Protocol tests

1. Valid single JSON-RPC request forwarded unchanged.
2. Invalid JSON returns parse error.
3. Missing `jsonrpc` rejected.
4. Batch array rejected.
5. Unknown extension method forwarded.
6. Unknown `_meta` preserved.
7. Valid `Mcp-Method` accepted.
8. Missing required `Mcp-Method` rejected.
9. Mismatched `Mcp-Method` rejected.
10. Missing required `Mcp-Name` rejected.
11. Mismatched `Mcp-Name` rejected.
12. Header names compared case-insensitively.
13. Header values compared case-sensitively.
14. Recognized `Mcp-Param-*` mismatch rejected.
15. Unsupported protocol version rejected.
16. Modern request creates no bridge session.
17. Approved legacy initialize/session flow still works.

## 16.3 Policy tests

1. Allowed tool appears in `tools/list`.
2. Hidden tool does not appear.
3. Allowed tool call reaches upstream.
4. Hidden tool call does not reach upstream.
5. Denied tool call does not reach upstream.
6. Modern forged header cannot bypass body-based policy.
7. Legacy request policy uses parsed body.
8. Policy-filtered response removes stale entity metadata.
9. Different principals do not share unsafe discovery cache.
10. Policy update takes effect without unrelated process restart.

## 16.4 Security tests

1. Invalid Origin returns `403`.
2. Valid or absent Origin follows configured policy.
3. Invalid Host rejected.
4. Default non-loopback exposure prohibited.
5. Missing API key rejected in authenticated mode.
6. Invalid API key rejected.
7. Caller `Authorization` not forwarded.
8. Endpoint upstream authorization is injected.
9. Configured secrets absent from captured logs.
10. Managed process is not started for rejected caller.
11. Untrusted forwarded identity ignored.
12. Body-size limit enforced before parsing.

## 16.5 Proxy and SSE tests

1. JSON response status and safe headers preserved.
2. Decoded modified response strips stale encoding and length.
3. Multiline SSE data preserved.
4. SSE comments preserved.
5. SSE ID and retry fields preserved.
6. Client disconnect closes upstream.
7. Upstream malformed event closes cleanly.
8. No full stream buffering.
9. Shared HTTP client opens and closes with application lifespan.
10. Upstream timeout maps to expected gateway response.

## 16.6 Runtime tests

1. Concurrent first requests start one process.
2. Process transitions through expected states.
3. TCP-open but non-MCP service fails readiness.
4. MCP discovery success marks runtime ready.
5. Legacy fallback readiness works where approved.
6. Active stream holds a lease.
7. Idle timeout does not stop active process.
8. Final lease release begins idle countdown.
9. Idle client connection alone does not refresh activity.
10. Unexpected process exit marks runtime failed.
11. Restart backoff is bounded.
12. Reload waits for old process shutdown.
13. Port is not reused prematurely.
14. Application shutdown leaves no process or task.

## 16.7 Legacy tests

1. Bridge disabled by default.
2. Explicit bridge creates one bounded session.
3. Cross-endpoint reuse rejected.
4. Queue overflow follows configured behavior.
5. Session TTL cleanup works.
6. Session-count limit works.
7. Disconnect cancels response task.
8. Disconnect closes upstream stream.
9. Endpoint removal closes all endpoint sessions.
10. Repeated connect/disconnect does not increase task count or memory materially.

## 16.8 Integration matrix

Each matrix cell must run against a deterministic local fixture before external service smoke tests.

| Client path           | Upstream path                      | Required                      |
| --------------------- | ---------------------------------- | ----------------------------- |
| Modern 2026 stateless | Modern remote                      | Yes                           |
| Modern 2026 stateless | Modern managed                     | Yes                           |
| Legacy sessionful     | Legacy Streamable HTTP             | If approved in Phase 0        |
| Legacy HTTP+SSE       | Legacy HTTP+SSE                    | If approved                   |
| Legacy SSE client     | Explicit bridge to Streamable HTTP | If an actual consumer remains |
| Stdio adapter         | Stdio fixture                      | Only if stdio is implemented  |

---

# 17. CI requirements

Required checks:

```text
ruff-format
ruff-lint
static-type-check
unit-tests-py313
unit-tests-py314
integration-tests-py313
integration-tests-py314
dependency-audit
mcp-conformance
all-green
```

The agent must:

1. run applicable checks locally before pushing;
2. open a draft PR;
3. use:

```bash
gh pr checks --watch --fail-fast
```

4. wait for the command to complete;
5. inspect failing logs;
6. repair and repeat;
7. never claim checks passed based on a separate premature status query.

Gate issues must link the final successful check run.

---

# 18. Historical coding-agent operating instructions

The instructions in this section capture the original implementation-plan workflow. They are non-authoritative where they conflict with the repository's current Central/local-agent workflow, exact-head evidence requirements, or `docs/validation.md`.

## 18.1 Before editing

The coding agent must:

1. Read this PRD.
2. Read the assigned phase issue.
3. Read all dependency issues and relevant gate issue.
4. Inspect current repository files rather than relying only on issue descriptions.
5. Run the existing tests.
6. Record baseline failures separately from introduced failures.
7. Identify exact files and interfaces expected to change.
8. State any PRD/repository/spec conflict before implementation.

## 18.2 During implementation

The agent must:

- make the smallest coherent change satisfying the assigned slice;
- preserve endpoint-per-server architecture;
- avoid unrelated cleanup;
- add or update tests with each behavior change;
- avoid weakening assertions merely to make tests pass;
- keep protocol parsing separate from policy decisions;
- keep policy separate from transport forwarding;
- keep runtime ownership explicit;
- avoid detached `asyncio.create_task()` calls without registry ownership;
- avoid broad exception swallowing;
- avoid logging secret-bearing objects;
- use typed models and explicit enums;
- document any intentional compatibility break.

## 18.3 Prohibited implementation shortcuts

The agent must not:

1. Replace the entire router with a monolithic MCP server.
2. Treat `tools/list` filtering as authorization.
3. Trust `Mcp-Method` or `Mcp-Name` without body validation when the body is parsed.
4. Repair malformed JSON-RPC.
5. accept batches for convenience.
6. create one `httpx.AsyncClient` per request.
7. use unbounded queues.
8. use unmanaged background tasks.
9. retain `create_subprocess_shell()` as the default process API.
10. mark TCP-open as complete MCP readiness.
11. silently preserve unsupported `stdio_bridge`.
12. add unrelated MCP extensions.
13. close a gate without executable evidence.
14. merge while required checks are still pending.

## 18.4 PR requirements

Each PR description must include:

```markdown
## Issue and phase

Closes/Advances #...

## Requirements implemented

- REQ-ID
- REQ-ID

## Architecture impact

...

## Compatibility impact

...

## Security impact

...

## Tests added or changed

...

## Commands run

...

## Remaining work

...

## Gate evidence

...
```

Every PR must identify:

- which requirements it implements;
- which requirements remain;
- whether it changes compatibility;
- whether it changes the threat model;
- whether configuration migration is required.

---

# 19. Phase and gate completion

## Gate A — architecture and dependency readiness

Required:

- Phase 0 complete;
- Phase 1 complete;
- ADR approved;
- compatibility matrix committed;
- selected official SDK version demonstrated;
- stale dependency surfaces removed;
- typed config active;
- startup/reload validation tests pass.

## Gate B — protocol and security readiness

Required:

- Phase 2 complete;
- Phase 3 complete;
- Gate A closed;
- malformed request rejection verified;
- modern header validation verified;
- tool-call bypass closed;
- Origin/Host/authentication tests pass;
- credential isolation verified;
- SSE cancellation verified.

## Gate C — runtime and legacy readiness

Required:

- Phase 4 complete;
- Phase 5 complete;
- Gate A closed;
- runtime state and lease behavior verified;
- reload races closed;
- readiness verified;
- process/task cleanup verified;
- legacy queues and sessions bounded;
- stdio decision complete.

## Gate D — release readiness

Required:

- Phase 6 complete;
- Gates A, B, and C closed;
- compatibility matrix passes;
- conformance passes;
- security and leak regressions pass;
- Python 3.13 and 3.14 CI passes;
- documentation complete;
- release PR checks complete;
- no unresolved blocker remains.

---

# 20. Migration and rollback

## 20.1 Configuration migration

The release must include:

- before/after configuration examples;
- mapping of old `mode`, `command`, `url`, and transport fields;
- migration for legacy shell commands;
- explanation of new security defaults;
- explanation of unsupported modes;
- explanation of canonical route behavior.

## 20.2 Runtime rollout

Recommended rollout:

1. Deploy in local-only mode.
2. Run mock integration matrix.
3. Smoke-test each configured real upstream.
4. Verify allowlist enforcement using a deliberately denied direct call.
5. Verify no secret-bearing headers reach a test upstream.
6. Verify managed idle shutdown.
7. Verify hot reload.
8. Enable non-local exposure only after authenticated-mode tests pass.

## 20.3 Rollback

Before release:

- retain a tagged or otherwise reproducible `v0.1.x` state;
- preserve a copy of the prior configuration;
- document downgrade steps;
- avoid destructive persistent-state migrations;
- ensure runtime data created by `v0.2.0` can be discarded without affecting upstream MCP services.

---

# 21. Open decisions

These decisions must be resolved in the identified phase.

## OD-1: Exact Python SDK v2 version

**Phase:** 1
**Status:** RESOLVED
**Resolution:** `mcp>=2,<3` with committed `uv.lock` resolving `mcp==2.1.1`; Issue #4 and Gate A (#10) closed with evidence.

## OD-2: Actual legacy bridge consumers

**Phase:** 0
**Status:** RESOLVED FOR BASELINE
**Resolution:** no concrete client requiring the optional local SSE bridge was identified; ADR-001 records it as provisionally unused pending new deployment evidence.

## OD-3: Initial non-local authentication mode

**Phase:** 3
**Recommended default:** gateway API key plus trusted reverse-proxy support.

## OD-4: Stdio support

**Phase:** 5
**Decision:** full official SDK adapter or complete removal from accepted config.

## OD-5: Backward-compatible configuration parsing

**Phase:** 1
**Recommended approach:** provide explicit migration and limited legacy-field parsing only where it does not retain unsafe shell behavior.

## OD-6: Discovery caching

**Phase:** 3 or 6
**Recommended approach:** no cross-request cache until principal and policy partitioning are proven.

---

# 22. Requirement traceability

| Requirement area                        | Primary issue | Gate |
| --------------------------------------- | ------------: | ---: |
| Architecture and compatibility baseline |            #3 |  #10 |
| Dependencies and typed configuration    |            #4 |  #10 |
| JSON-RPC and protocol edge              |            #5 |  #11 |
| Standard MCP headers                    |            #5 |  #11 |
| Caller authentication and Origin/Host   |            #6 |  #11 |
| Capability policy                       |            #6 |  #11 |
| Runtime state and leases                |            #7 |  #12 |
| Managed processes and reload            |            #7 |  #12 |
| Legacy adapters and stdio               |            #8 |  #12 |
| Conformance, CI, observability          |            #9 |  #13 |
| Documentation and release               |            #9 |  #13 |

---

# 23. Definition of done

The refactor is complete only when:

- all Phase 0–6 issues are closed;
- all Gate A–D issues are closed with linked evidence;
- the official selected MCP SDK v2 version is locked;
- the canonical modern route is `/<namespace>`;
- modern traffic does not use local protocol sessions;
- approved legacy traffic passes the compatibility matrix;
- denied direct tool calls never reach upstream;
- Origin, Host, authentication, and credential-isolation tests pass;
- malformed JSON-RPC and batches are rejected;
- standard routing headers are validated;
- SSE framing and cancellation pass;
- managed process reload and idle behavior pass concurrency tests;
- no subprocess, task, queue, stream, or HTTP client leak remains;
- every accepted endpoint kind has integration coverage;
- official conformance scenarios pass for supported behavior;
- CI is green on Python 3.13 and 3.14;
- documentation accurately describes implemented behavior;
- version and release notes are ready for `v0.2.0`.

---

# 24. Deliverable self-check

Before treating this PRD as satisfied, verify:

- [ ] No requirement depends on response-only tool filtering as authorization.
- [ ] No modern protocol request is assigned a mux-generated session.
- [ ] No malformed JSON-RPC is repaired.
- [ ] No required routing header is trusted without validation.
- [ ] No upstream credential is available to an unauthenticated caller.
- [ ] No default path uses wildcard credentialed CORS.
- [ ] No background task lacks an owner and cleanup path.
- [ ] No queue or session registry is unbounded.
- [ ] No managed process is considered ready from TCP alone.
- [ ] No configuration reload publishes partial state.
- [ ] No accepted endpoint mode is nonfunctional.
- [ ] No gate is closed without executable evidence.
- [ ] No external SDK version is asserted without verification.
