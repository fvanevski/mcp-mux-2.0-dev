# Endpoint configuration contract

`mcp_router/config.yaml` is validated before an endpoint definition can become active. The accepted endpoint modes are intentionally limited to behavior implemented by the router: `remote` and `managed_cli`. Historical `stdio_bridge` configuration is rejected.

## Common endpoint fields

Every endpoint requires:

- `path`: one route namespace matching `[A-Za-z0-9][A-Za-z0-9._-]*`. `summary` is reserved case-insensitively and `/` is forbidden.
- `mode`: `remote` or `managed_cli`.
- `url`: an absolute `http` or `https` URL.
- `summary`: a non-empty operator-facing description.

Optional common fields are `timeout`, `transport`, `legacy_sse_bridge`, `headers`, `allowed_tools`, and `denied_tools`. `timeout` must be positive. `transport`, when explicit, is `sse` or `streamable-http`; otherwise it is inferred from the URL. `legacy_sse_bridge` is valid only for `streamable-http` endpoints. Tool names must be non-empty and unique within their list. If both allow and deny lists are present, the allowlist continues to take precedence.

Configuration models reject unknown fields. Environment references are expanded before model validation using `${NAME}` for required values or `${NAME:-fallback}` for a default.

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

`readiness.host` and `readiness.port` default from `url`; `timeout` defaults to 15 seconds and `interval` to 0.2 seconds. Readiness polling uses a monotonic deadline: after a failed connection probe, the next sleep is capped to the remaining timeout budget, so an `interval` longer than `timeout` cannot extend the polling delay past that deadline. Managed readiness ports must be unique across the loaded configuration.

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

Hot reload is last-known-good: a changed file is parsed and validated before the router callback is invoked. If parsing, environment expansion, or validation fails, the candidate configuration is logged and rejected and the active configuration remains unchanged. A subsequent file change is evaluated normally.

## Dependency authority

Project dependencies are declared in the repository-root `pyproject.toml` and resolved in `uv.lock`. There is no secondary hand-maintained `mcp_router/requirements.txt`. The MCP runtime dependency is the official `mcp` package on major version 2; test-only dependencies remain in the development dependency group.
