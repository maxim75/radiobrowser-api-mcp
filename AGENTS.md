# AGENTS.md

Single-purpose repo: `radiobrowser-api-mcp` — an MCP server wrapping the
Radio Browser directory API plus live ICY/Shoutcast "now playing" metadata.

## Layout

- `server.py` — all 29 MCP tools. Single source of truth; `mcp` object + `_api_get`/`_api_post` helpers + `KNOWN_STATIONS` curated fallbacks + `MUTATING_TOOLS` + `_require_mutating_auth`.
- `app.py` — ASGI app (`create_app(host)`): Streamable HTTP at `/mcp`, legacy SSE at `/sse`+`/messages`, open `/health` and `/`.
- `test_server.py` — network-free unit tests for `get_now_playing` semantics.
- `test_http_transport.py` — transport tests: routes, CLI, uvicorn dispatch, auth matrix driven off `MUTATING_TOOLS`.
- `main.py` — leftover `uv init` template, irrelevant.

## Commands

```sh
uv run server.py                                             # stdio (default)
uv run server.py --transport streamable-http --http-port 50052  # remote mode
uv run python -m py_compile server.py app.py                 # verify
uv run pytest -q && uv run ruff check .                      # test + lint before pushing
uv run python -c "import asyncio, server; ..."  # smoke test via server.mcp.call_tool(...)
```

- Runtime is Python 3.12 (`.python-version`); system python is 3.14 — always use `uv run`.
- After editing `pyproject.toml` by hand, run `uv lock`.
- Dev deps (`pytest`, `ruff`) live in the `dev` dependency group — `uv sync --group dev` (or `uv run pytest` resolves them); the Docker image installs runtime only.

## MCP SDK v2 quirks

- Installed SDK is `mcp>=2.1.1`: import is `mcp.server.mcpserver.MCPServer` (`FastMCP` was renamed). `server.py` already has a v1/v2 compat shim — keep it.
- Tool results arrive as a list of content blocks (one `TextContent` per item, single dicts wrapped in one block). Any test/parsing helper must handle that.
- Inspector: run `npx @modelcontextprotocol/inspector` and open the full terminal URL including `?MCP_PROXY_AUTH_TOKEN=...` — bare `localhost:6274` returns "Unauthorized".

## Radio Browser API (verified by live probing, not docs)

- Base mirrors: `de1/de2/nl1.api.radio-browser.info`; `_api_get` fails over across them. Always send a `User-Agent`.
- `/json/countries` and `/json/countrycodes` ignore filter params — expose as base-list only.
- Facet search supports `byname/bycountry/bystate/bylanguage/bytag/bycodec` + `*exact` variants; country codes only via `bycountrycodeexact`. `GET /json/checks?stationuuid=...` works; `/json/stationchecks/{uuid}` 404s.
- Probe a new endpoint live before wrapping it.

## `get_now_playing` semantics (do not weaken)

- `ranked_search` = directory fuzzy search + `KNOWN_STATIONS` curated StreamTheWorld URLs (Radio Browser has no usable "Smooth FM Sydney" entry).
- Never answer with a *different* station's track: if the matched station connects but sends no `StreamTitle` (ad/news break), return track fields as `null`, not the next candidate's song. ICY scan window scales with bitrate × timeout.
- Triton's `np.tritondigital.com` now-playing endpoint returned empty for these mounts — use ICY parsing.

## Safe verification

- `register_station_click`, `vote_for_station`, `resolve_station_stream_url`, `add_station` mutate public directory counters — never call them in smoke tests. Use `get_directory_stats`, facet lists, and read-only station tools.

## Error surfacing (MCP SDK v2)

- SDK wraps non-`ToolError` exceptions in `UnexpectedToolError` whose message is only `Error executing tool <name>`; detail survives in `__cause__`. Anticipated failures must raise `ToolError` (import under the v1/v2 shim).
- `get_now_playing` semantics: ranked curated fallback (>= 2 keyword overlap) + same-station loop gate (>= 2 `_station_tokens` overlap, parens stripped) + direct-streams-first sort (non-HLS scores higher under `reverse=True`). `test_server.py` pins all three — run `uv run pytest -q` and `uv run ruff check .` before pushing.

## Client config gotcha (macOS GUI clients)

GUI apps don't inherit shell `PATH`: use the full uv path (`/Users/maksym/.local/bin/uv`) or the venv interpreter (`.venv/bin/python`) as the command, with one argument per line.
