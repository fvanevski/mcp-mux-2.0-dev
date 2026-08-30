# Endpoint configuration contract

`mcp_router/config.yaml` is validated before an endpoint definition can become active. The accepted endpoint modes are intentionally limited to behavior implemented by the router: `remote` and `managed_cli`. Historical `stdio_bridge` configuration is rejected.

At router scope, `max_request_body_bytes` bounds JSON-RPC POST bodies before they can be forwarded. It defaults to 1 MiB (`1048576`) and accepts values from 1 KiB through 64 MiB. The limit applies after any declared `Content-Length` precheck and again while the actual request body is streamed.

## Gateway security

The default security mode is `local_only`. The shipped configuration makes that default explicit:

```yaml
security:
  mode: local_only
  allowed_hosts: [127.0.0.1, localhost, "::1"]
  allowed_origins: []
```

`local_only` requires a loopback immediate peer, validates the `Host` header against `allowed_hosts`, and rejects every present `Origin` that is not explicitly listed. The command-line launcher also refuses a non-loopback `--host` while this mode is active. There is no wildcard credentialed CORS path. Do not place `local_only` behind an externally reachable reverse proxy: a same-host proxy is itself a loopback peer and therefore cannot preserve the local-only trust boundary. Any externally reachable or reverse-proxied deployment must use `authenticated` mode.

Non-local binding requires `security.mode: authenticated` and at least one explicit authentication provider. Direct callers may authenticate with a gateway API key supplied as `Authorization: Bearer <key>`:

```yaml
security:
  mode: authenticated
  allowed_hosts: [mcp.example.test]
  allowed_origins: [https://agent.example.test]
  api_key: "${MCP_MUX_API_KEY}"
```

The gateway consumes that `Authorization` value locally and never forwards it to the MCP upstream. Endpoint `headers` remain the separate source for credentials injected toward the upstream.

A trusted reverse proxy may instead assert an identity header, but only when the immediate network peer is inside an explicitly configured IP/CIDR range:

```yaml
security:
  mode: authenticated
  allowed_hosts: [mcp.example.test]
  trusted_proxies: [127.0.0.1/32]
  trusted_proxy_identity_header: X-Forwarded-User
```

The reverse proxy must terminate external authentication, overwrite the configured identity header rather than append to caller input, and connect to the mux from the configured trusted address. Forwarded identity from any other peer is ignored. Do not configure an Internet-facing proxy address range more broadly than necessary.

Browser origins remain exact allowlist entries even in authenticated mode. A valid preflight receives an exact `Access-Control-Allow-Origin`; the router never emits `*` together with credentials.

## Common endpoint fields

Every endpoint requires:

- `path`: one route namespace matching `[A-Za-z0-9][A-Za-z0-9._-]*`. `summary` is reserved case-insensitively and `/` is forbidden.
- `mode`: `remote` or `managed_cli`.
- `url`: an absolute `http` or `https` URL.
- `summary`: a non-empty operator-facing description.

Optional common fields are `timeout`, `upstream_timeout`, `transport`, `legacy_sse_bridge`, `headers`, `inbound_headers`, capability-policy lists, `limits`, and `tool_limits`. `timeout` is the managed-endpoint inactivity timeout measured from completion of the last real upstream-work lease; an idle local compatibility SSE connection does not refresh it. `upstream_timeout` defaults to 60 seconds and bounds upstream request establishment plus finite response consumption: connect/write/pool operations remain bounded, response headers must arrive within the configured budget, complete JSON responses must finish within that budget, and non-SSE streamed responses retain a per-chunk idle bound. Event-stream responses deliberately have no read-idle timeout because both modern Streamable HTTP and explicit legacy HTTP+SSE may remain valid while quiet; downstream cancellation still closes the corresponding upstream response context. Both configuration values must be positive. `transport`, when explicit, is `sse` or `streamable-http`; otherwise it is inferred from the URL. `legacy_sse_bridge` is valid only for `streamable-http` endpoints.

Capability policy supports mutually exclusive allow/deny pairs for methods, tools, resources, and prompts: `allowed_methods`/`denied_methods`, `allowed_tools`/`denied_tools`, `allowed_resources`/`denied_resources`, and `allowed_prompts`/`denied_prompts`. Names must be non-empty and unique. Supplying both halves of any pair is a configuration error; the loader no longer silently discards a denylist. The same policy evaluator authorizes direct calls and projects discovery results, so a capability omitted from discovery cannot still be invoked by name. Resource policy covers `resources/read`, legacy `resources/subscribe`/`resources/unsubscribe`, and every URI requested through modern `subscriptions/listen` `notifications.resourceSubscriptions`; a listen request that asks only for list-change notifications is evaluated only against method policy. Modern requests reach policy only after Phase-2 header/body validation; supported legacy requests use the parsed JSON-RPC body as the policy source.

Configuration models reject unknown fields. Environment references are expanded before model validation using `${NAME}` for required values or `${NAME:-fallback}` for a default.

### Caller headers versus upstream headers

Inbound caller headers are deny-by-default. MCP transport headers needed for protocol operation (`Content-Type`, `Accept`, MCP routing/session headers, `Mcp-Param-*`, and trace context) are forwarded automatically. An endpoint may opt additional non-security headers in with `inbound_headers`:

```yaml
inbound_headers: [X-Tenant-Hint]
headers:
  Authorization: "Bearer ${UPSTREAM_TOKEN}"
```

`Authorization`, `Cookie`, `Proxy-Authorization`, Host/hop-by-hop headers, and forwarded-identity headers cannot be added to `inbound_headers`. The caller's credentials are stripped before upstream dispatch; configured `headers` are applied afterward as the upstream credential/header source. The configured trusted-proxy identity header is also removed before forwarding.

Normal logs redact configured API keys, credential-bearing upstream header values, secret-like managed-process environment values, and secret-like assignments in unstructured log text. Protocol response bodies use literal known-secret replacement rather than assignment-pattern rewriting so JSON/SSE framing and ordinary text such as `token=example` remain intact. Upstream text/JSON/SSE responses are redacted against the known secret set before being returned, upstream `Set-Cookie` is not exposed, and otherwise permitted response headers are dropped when their values contain a configured secret. Policy- or redaction-transformed responses drop stale representation/cache validators such as `Content-Length`, `Content-Encoding`, `ETag`, and digest metadata and are marked non-cacheable where applicable. Configuration validation suppresses raw candidate inputs in model errors and normalizes malformed-YAML errors so rejected credential-bearing configuration does not disclose its values through ordinary validation logs. The mux currently implements no cross-request discovery cache; any future cache must partition by endpoint, principal/authorization scope, policy identity, and protocol revision.

### Endpoint and tool limits

Per-endpoint limits are optional and in-process:

```yaml
limits:
  max_concurrent: 32
  requests_per_minute: 600
tool_limits:
  expensive_tool:
    max_concurrent: 2
    requests_per_minute: 30
```

An exceeded limit is rejected locally before managed-process activation or upstream dispatch. Streaming responses hold their endpoint concurrency lease until the downstream stream closes. Buffered upstream read failures release an acquired concurrency lease before the exception propagates, so a transient upstream failure cannot permanently consume endpoint capacity. Rate windows are one minute and are scoped independently per endpoint and configured tool.

## Remote endpoint

```yaml
- path: "context7"
  mode: "remote"
  url: "https://mcp.context7.com/mcp"
  headers:
    Authorization: "Bearer ${CONTEXT7_TOKEN:-}"
  summary: "Context7 documentation lookup and indexing system"
```

## Managed endpoint

Managed execution is explicit. Prefer structured `argv`; it is executed directly without a shell.

```yaml
- path: "example"
  mode: "managed_cli"
  argv: ["example-mcp", "--serve"]
  env:
    PORT: "3033"
  cwd: "/srv/example"
  readiness:
    host: "localhost"
    port: 3033
    timeout: 15
    interval: 0.2
  url: "http://localhost:3033/mcp"
  summary: "Example managed MCP server"
```

`readiness.host` and `readiness.port` default from `url`; `timeout` defaults to 15 seconds and `interval` to 0.2 seconds. Readiness uses one monotonic startup deadline and requires both TCP connectivity and a successful MCP `2026-07-28` `server/discover` result before the runtime becomes `RUNNING`. Each MCP probe receives the remaining startup budget; `interval` controls spacing between failed probes rather than imposing a smaller response deadline. Readiness requests preserve configured upstream `headers` such as authentication credentials while the mux authoritatively supplies the required MCP transport headers. A `text/event-stream` readiness response is consumed incrementally and succeeds on the first complete valid JSON-RPC result without waiting for stream EOF. `legacy_initialize_fallback: true` permits an explicit legacy `initialize` probe only after modern discovery fails. TCP-open alone is never sufficient. Managed target URLs are loopback-only by default; `allow_non_loopback_target: true` is the explicit trusted-configuration escape hatch. Managed readiness ports must be unique across the loaded configuration.

`restart.enabled` defaults to `false`. An unexpected process exit transitions the endpoint runtime to `FAILED`. A failed runtime rejects demand activation, so incoming request pressure cannot bypass backoff or reset the restart-attempt budget. When automatic restart is enabled, the supervisor alone advances recovery using bounded exponential backoff from `initial_backoff` through `max_backoff`, stopping after `max_attempts`; after exhaustion the runtime remains `FAILED` until a runtime-replacing configuration transition or application restart. Runtime state, active upstream leases, last exit code, and restart-attempt count are exposed by `/summary` for operational diagnosis.

Use `unsafe_shell_command` only when shell syntax is intrinsically required, such as sourcing a shell-managed runtime before launching a server. It is mutually exclusive with `argv`:

```yaml
- path: "firecrawl"
  mode: "managed_cli"
  unsafe_shell_command: "export NVM_DIR=$HOME/.config/nvm && [ -s $NVM_DIR/nvm.sh ] && . $NVM_DIR/nvm.sh && npx --yes firecrawl-mcp"
  env:
    HTTP_STREAMABLE_SERVER: "true"
    PORT: "3033"
    HOST: "localhost"
  url: "http://localhost:3033/mcp"
  summary: "Firecrawl Web Content Extraction Tool"
```

The shell escape hatch is deliberately named `unsafe_shell_command`; ordinary managed endpoints should use `argv` so arguments are not reinterpreted by a shell. The router does not log the shell command contents or managed environment values when starting a process.

## Migration from the legacy schema

Legacy managed configuration used an opaque `command` string and optionally separate `args` / `port` fields. Migrate it as follows:

1. Convert commands that do not need shell syntax to `argv`.
2. Move process environment values to `env` and a working directory to `cwd`.
3. Move readiness behavior to `readiness`; omit host/port when deriving them from `url` is correct.
4. Use `unsafe_shell_command` only for a command that truly requires shell operators, expansion, or sourcing.
5. Remove `command`, `args`, `port`, and `stdio_bridge`; these legacy fields/modes are rejected rather than retained as compatibility paths.

## Startup and reload semantics

Initial configuration is loaded, environment-expanded, and fully validated before the watcher or idle-timeout runtime tasks are started. Invalid initial configuration therefore prevents application startup.

Hot reload is last-known-good: a changed file is parsed and validated before the router callback is invoked. If parsing, environment expansion, or validation fails, the failure is logged without raw candidate input, the candidate is rejected, and the active configuration remains unchanged. A subsequent file change is evaluated normally.

Configuration application is asynchronous and serialized. For removed or runtime-affecting endpoints the old `EndpointRuntime` first enters `DRAINING`, rejects new upstream leases, waits for existing upstream work to finish, and awaits managed-process shutdown and owned task cleanup. A retired runtime remains `DRAINING` even after its process has stopped while the old registry is still published; this prevents an already-drained endpoint from being reactivated if another endpoint is still draining. Only after every retired runtime is quiescent is the complete registry/config/security snapshot published, without an intervening await. Request handlers therefore cannot observe a partially applied registry, and a replacement that reuses the same port cannot race or orphan a process from the old configuration.

Security, policy, inbound-header, upstream-header, timeout, restart-policy, and request-limit changes are applied in place. They do not restart an otherwise unchanged managed process or discard a compatibility session. Changes that alter the endpoint target, transport/bridge mode, managed argv/environment/working directory, readiness settings, or unsafe shell command remain runtime-affecting and drain/reset the existing runtime/session state for that endpoint.

## Dependency authority

Project dependencies are declared in the repository-root `pyproject.toml` and resolved in `uv.lock`. There is no secondary hand-maintained `mcp_router/requirements.txt`. The MCP runtime dependency is the official `mcp` package on major version 2; test-only dependencies remain in the development dependency group.
