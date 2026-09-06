# RadioBrowser API — MCP server

An MCP server for the [Radio Browser API](https://www.radio-browser.info/)
plus live "now playing" track info read from station streams via
ICY/Shoutcast metadata. No API keys needed.

## What is the Radio Browser API?

[Radio Browser](https://www.radio-browser.info/) is a free, community-driven
directory of internet radio stations — currently ~58,000 stations
(57,973 at last check) with ~12,000 tags. Anyone can submit stations, and
automated jobs continuously probe stream health (`lastcheckok`). It exposes a
free JSON webservice with no API key; the only requirement is sending a
`User-Agent` header. The service runs on several mirror servers, which this
project fails over across automatically:

- `https://de1.api.radio-browser.info`
- `https://de2.api.radio-browser.info`
- `https://nl1.api.radio-browser.info`

Useful links:

- Directory search UI: <https://www.radio-browser.info/>
- Webservice docs (endpoint reference): <https://www.radio-browser.info/webservice>
- Server implementation (Rust): <https://github.com/segler-alex/radiobrowser-api-rust>
- Web frontend: <https://github.com/segler-alex/radiobrowser-web-angular>

What this project adds on top: every directory endpoint as an MCP tool, plus
`get_now_playing`, which the directory itself cannot provide — it resolves a
station name to a stream URL and reads the live track from the stream's
ICY/Shoutcast metadata.

## Tools (29)

Live track info:
- `get_now_playing(station_name, timeout=20)` — current track from the
  station's stream via ICY/Shoutcast metadata. Returns
  `{artist, title, raw_title, station_matched, country, stream_url}`;
  track fields are `null` when the station sends no titles right now.

Directory search (`search_radio_stations`, `advanced_station_search`,
`list_all_stations`, `get_station_by_uuid`, `find_stations_by_name`,
`find_stations_by_country`, `find_stations_by_country_code`,
`find_stations_by_state`, `find_stations_by_language`,
`find_stations_by_tag`, `find_stations_by_codec`) — mirrors of
`GET /json/stations[/search|/byuuid|/by*]`, with `exact`, `order`,
`reverse`, `offset`, `limit`, `hidebroken` params.

Rankings & history: `get_top_voted_stations`, `get_most_clicked_stations`,
`get_recently_clicked_stations`, `get_recently_updated_stations`
(`topvote/topclick/lastclick/lastchange`), `get_station_check_history`
(`GET /json/checks` — the API ignores `limit`, so bound with `seconds`).

Counters & submission: `register_station_click` (`/click`), `vote_for_station`
(`/vote`), `resolve_station_stream_url` (`/url`), `add_station` (`POST /add`).
Note: click/vote/url increment public counters.

Facets & meta: `list_countries`, `list_country_codes`, `list_codecs`,
`list_states`, `list_languages`, `list_tags`, `get_directory_stats`,
`list_directory_servers`.

## Run

```sh
uv run server.py                                # stdio transport (default)
uv run server.py --transport grpc --port 50051  # gRPC for remote connections
```

gRPC exposes all 29 tools via `CallTool(name, arguments_json)` /
`ListTools()` — see `radio_mcp.proto`. The bridge is a generic JSON
pass-through, so new MCP tools need no gRPC-side changes. `CallTool` results
are wrapped in an explicit envelope `{"items": [...], "count": N}` so clients
can tell an object result apart from a one-item list. Regenerate stubs
only when the `.proto` changes:

```sh
uv run python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. radio_mcp.proto
```

### gRPC auth (remote deployments)

`register_station_click`, `vote_for_station`, `resolve_station_stream_url`
and `add_station` mutate the public directory. When `RADIO_MCP_AUTH_TOKEN`
is set, calls to these tools must carry
`authorization: Bearer <token>` gRPC metadata; read-only tools stay open.
Unset (default) means everything is open — fine for localhost, not for the
internet.

## Deploy (Docker / Coolify)

The image runs the server in gRPC mode on port 50051. Coolify accepts a
plain `docker-compose.yml`, so deployment is: new Resource → Docker Compose →
point it at this repo.

```sh
docker compose up -d --build
```

- Host port is configurable: `GRPC_PORT=50099 docker compose up -d` maps
  host `50099` → container `50051` (the in-container port is fixed).
  The host bind is loopback-only; Coolify overrides networking itself.
- Health: the server registers the standard `grpc.health.v1` service and the
  image runs `healthcheck.py` every 30s (`HEALTHCHECK` + compose
  `healthcheck`, so Coolify shows status). Probe manually with
  `uv run python healthcheck.py` (uses `HEALTHCHECK_PORT`, default 50051).
- The container runs as non-root `appuser` on `python:3.12-slim`, matching
  local dev (`.python-version`).
- For remote/Coolify deployments, set `RADIO_MCP_AUTH_TOKEN` (Coolify env
  vars) so the mutating tools require a Bearer token (see gRPC auth above).
- The image installs runtime deps only (`uv sync --frozen --no-dev`) and
  ships pre-generated protobuf stubs, so no build tools are needed at deploy.
- Uses insecure (plaintext) gRPC — put it behind a private network or a
  TLS-terminating reverse proxy; do not expose it directly to the internet.
- Note: the default host port 50051 may collide if something already listens
  there (seen locally); set `GRPC_PORT` to avoid it.

## Client config (example)

```json
{
  "mcpServers": {
    "radio-now-playing": {
      "command": "uv",
      "args": ["--directory", "/Users/maksym/code/open_code_test", "run", "server.py"]
    }
  }
}
```
