# Serving MCP over HTTP, replacing the gRPC bridge

## Why

The deployment currently exposes a gRPC service. No MCP client can use it —
MCP's transports are stdio, Streamable HTTP and SSE, and gRPC is none of them.
Claude Desktop, Claude Code, opencode, Cursor and Zed all connect over HTTP or
stdio, so the deployed server is unreachable from every tool it exists to serve.
Only hand-written gRPC clients could call it, and there are none.

Replacing gRPC with MCP-over-HTTP makes the deployment usable by ordinary MCP
clients and removes a parallel server implementation.

The architecture mirrors `maxim75/spotify-mcp-mx`, which already serves MCP over
HTTP on the same Coolify host.

## Scope

In scope: the HTTP transport, the auth model on that transport, removal of gRPC,
and the deployment configuration.

Out of scope, deliberately:

- Splitting `server.py` into a package. It is ~968 lines and would benefit, but
  a mechanical move of 29 tool definitions on top of a transport change makes
  regressions hard to attribute. Separate piece of work.
- The reference repo's multi-stage build and non-root container user. Both are
  improvements; neither is a transport concern.

## Architecture

### `app.py` (new)

Owns the ASGI application. Modelled directly on `spotify_mcp_mx/app.py`.

```
create_app(host) -> Starlette
    app = mcp.streamable_http_app(stateless_http=True, host=host)
    append routes from mcp.sse_app(host=host) that are not already mounted
```

`streamable_http_app()` is the base because it owns the lifespan that runs the
session manager. SSE routes are self-contained and can be appended.

Routes:

| Path         | Purpose                                        | Auth |
|--------------|------------------------------------------------|------|
| `/mcp`       | Streamable HTTP transport (current standard)   | per-tool |
| `/sse`       | Legacy SSE transport, `/messages/` for posts   | per-tool |
| `/health`    | Liveness probe for Coolify and Docker          | open |
| `/`          | Short service description                      | open |

`/health` and `/` are registered with `@mcp.custom_route(...)`.

`stateless_http=True`: no MCP session is pinned to a worker, so Coolify can
restart or scale the container without breaking connected `/mcp` clients.

`host` is passed to the SDK so it can decide about DNS-rebinding protection. The
SDK auto-enables a localhost-only `Host`/`Origin` allowlist when told it is
serving loopback; behind Coolify's proxy the request arrives carrying the public
domain, which such an allowlist would reject. Binding `0.0.0.0` correctly leaves
it off.

`HOST` and `PORT` come from the environment, defaulting to `0.0.0.0` and
`50052`. They supply the defaults for the existing `--host` / `--http-port`
CLI flags, so an explicit flag always wins over the environment and there is
exactly one resolution order.

### SDK compatibility

The installed SDK is **mcp 2.1.1**, where `FastMCP` was renamed `MCPServer`.
`server.py` already handles both via a guarded import. The three calls this
design depends on were verified present in 2.1.1 with the signatures the
reference repo uses:

```
streamable_http_app(*, streamable_http_path='/mcp', stateless_http=False,
                    host='127.0.0.1', ...) -> Starlette
sse_app(*, sse_path='/sse', message_path='/messages/', host='127.0.0.1', ...) -> Starlette
custom_route(path, methods, name=None, include_in_schema=True)
```

### `server.py` (modified)

`--transport` drops `grpc`, keeping `stdio` (default) and `streamable-http`.
The HTTP branch runs uvicorn against `create_app()` rather than calling
`mcp.run(transport="streamable-http", ...)`, so `/sse` and `/health` are served
too. Tool definitions are untouched.

`MUTATING_TOOLS` moves here from `grpc_server.py` and becomes the single source
of truth for which tools write to the public directory:

```
register_station_click, vote_for_station, resolve_station_stream_url, add_station
```

### Removed

`grpc_server.py`, `radio_mcp.proto`, `radio_mcp_pb2.py`, `radio_mcp_pb2_grpc.py`,
`test_grpc_server.py`, and the `grpcio` / `grpcio-tools` dependencies.

## Authentication

Read-only tools stay open; the four mutating tools require a token when one is
configured. This preserves the current gRPC semantics rather than changing the
security posture at the same time as the transport.

A helper reads the `authorization` header from
`ctx.request_context.request.headers` — `ServerRequestContext.request` carries
the HTTP request that the transport attached — and compares it against
`RADIO_MCP_AUTH_TOKEN` with `hmac.compare_digest`, the same constant-time
comparison used today. Each of the four mutating tools calls it first, via an
injected `Context` parameter.

Rules:

| Condition                            | Behaviour |
|--------------------------------------|-----------|
| `RADIO_MCP_AUTH_TOKEN` unset/empty   | everything open, as today |
| Token set, valid `Bearer` header     | allowed |
| Token set, missing or wrong header   | refused |
| No HTTP request in context (stdio)   | check skipped |

The stdio carve-out keeps local use unchanged; over HTTP a request is always
present, so it opens no hole on the deployed path.

### Why not MCP middleware

mcp 2.x exposes a `middleware` chain on `MCPServer` that could centralise this
in one place, keyed off the tool name. The SDK documents it as *"Provisional -
the signature may change in a 2.x minor release."* This is the boundary between
the public internet and writes to the Radio Browser directory; it should not
rest on a provisional API that can change under a patch bump.

The cost of the per-tool approach is drift: a fifth mutating tool could be added
without the check. A test iterates `MUTATING_TOOLS` and asserts each member
refuses a call with no token, so the list and the enforcement cannot diverge
silently.

## Deployment

Plain HTTP means Coolify's generated Traefik labels work unmodified. This
removes the entire custom-label apparatus added in PRs #6, #7 and #8:

- no custom Traefik labels
- no `loadbalancer.server.scheme=h2c`
- no `priority=1000` to outrank the generated router
- no external `coolify` network declaration, and therefore no
  `docker network create coolify` needed for a local `docker compose up`

`docker-compose.yml` keeps `expose`, `RADIO_MCP_AUTH_TOKEN`, and a healthcheck
polling `/health`.

`Dockerfile`: `EXPOSE 50052`, `CMD [".venv/bin/python", "server.py",
"--transport", "streamable-http", "--host", "0.0.0.0", "--http-port",
"50052"]` — keeping the existing image's explicit venv path — and a
`HEALTHCHECK` hitting `/health` with stdlib urllib (the image ships no curl).

In Coolify: re-enable "Generate default labels" and set the domain on port
50052. `RADIO_MCP_AUTH_TOKEN` stays set as an env var.

### Client configuration

opencode, and any other MCP client supporting remote servers:

```json
{
  "mcp": {
    "radio-browser": {
      "type": "remote",
      "url": "https://radiobrowser-api-mcp.d.imaxim.org/mcp",
      "enabled": true,
      "headers": { "Authorization": "Bearer <token>" }
    }
  }
}
```

The header is only needed to call the four mutating tools; reads work without it.

## Error handling

- Refused mutating call: the tool raises so the MCP client sees a tool error
  naming the missing or invalid token, rather than a silent no-op.
- `/health` never depends on the upstream Radio Browser API, so a Radio Browser
  outage does not make Coolify restart a healthy container.
- Upstream failures keep their existing behaviour; this design does not change
  tool internals.

## Testing

`test_grpc_server.py` is deleted. `test_http_transport.py` grows to cover:

1. `create_app()` builds and mounts `/mcp`, `/sse`, `/messages/`, `/health`, `/`.
2. `GET /health` returns 200 with no credentials.
3. CLI advertises `streamable-http` and no longer advertises `grpc`.
4. `main()` dispatches `streamable-http` to uvicorn with the right host/port.
5. Auth matrix, driven off `MUTATING_TOOLS` so it cannot drift:
   - read-only tool with no token → allowed
   - every mutating tool with no token → refused
   - every mutating tool with a wrong token → refused
   - every mutating tool with the correct token → allowed
   - token unset entirely → every mutating tool allowed

Tests run through Starlette's test client — no network, no live deployment.

## Verification

Local: ruff clean, full test suite green, `docker compose config` valid, and a
stdio handshake still listing 29 tools.

Deployed: `curl -sf https://<domain>/health`, then an MCP client connecting to
`/mcp` and listing tools. A mutating tool without the header must be refused.

## Risks

- **`request` is `None` on a transport that does not attach it.** The stdio
  carve-out is deliberate, but if a future transport also omits it, auth would
  silently skip. The tests pin the HTTP path specifically.
- **Removing gRPC is one-way** in the sense that any future typed client loses
  the proto contract. Accepted: nothing consumes it today, and git history keeps
  it recoverable.
- **The endpoint stays publicly readable.** Unchanged from today, and consistent
  with Radio Browser being a public directory, but worth stating: anyone with
  the URL can call the 25 read-only tools.
