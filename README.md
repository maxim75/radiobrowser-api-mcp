# RadioBrowser API — MCP server

Exposes the track currently playing on a radio station, read live from the
station's stream via ICY/Shoutcast metadata. No API keys needed.

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
(`GET /json/checks`).

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
pass-through, so new MCP tools need no gRPC-side changes. Regenerate stubs
only when the `.proto` changes:

```sh
uv run python -m grpc_tools.protoc -I. --python_out=. --grpc_python_out=. radio_mcp.proto
```

## Deploy (Docker / Coolify)

The image runs the server in gRPC mode on port 50051. Coolify accepts a
plain `docker-compose.yml`, so deployment is: new Resource → Docker Compose →
point it at this repo.

```sh
docker compose up -d --build
```

- Host port is configurable: `GRPC_PORT=50099 docker compose up -d` maps
  host `50099` → container `50051` (the in-container port is fixed).
- The image installs runtime deps only (`uv sync --frozen --no-dev`) and
  ships pre-generated protobuf stubs, so no build tools are needed at deploy.
- Uses insecure (plaintext) gRPC — put it behind a private network or a
  TLS-terminating reverse proxy; do not expose it directly to the internet.
- Note: Coolify's default `0.0.0.0:50051` host binding may collide if
  something already listens there (seen locally); set `GRPC_PORT` to avoid it.

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
