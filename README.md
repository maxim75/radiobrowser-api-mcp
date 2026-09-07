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
uv run server.py                                  # stdio transport (default)
uv run server.py --transport streamable-http --http-port 50052  # HTTP for remote connections
```

The HTTP mode serves Streamable HTTP at `/mcp` (current standard) plus the
legacy SSE transport at `/sse` (posts to `/messages/`), with open `/health`
and `/` endpoints. `HOST`/`PORT` env vars supply the `--host`/`--http-port`
defaults.

### HTTP auth (remote deployments)

`register_station_click`, `vote_for_station`, `resolve_station_stream_url`
and `add_station` mutate the public directory. When `RADIO_MCP_AUTH_TOKEN`
is set, calls to these tools must carry an
`Authorization: Bearer <token>` HTTP header; read-only tools stay open.
Unset (default) means everything is open — fine for localhost, not for the
internet. Over stdio (local use) the check is skipped.

## Deploy (Docker / Coolify)

The image runs the server in Streamable HTTP mode on port 50052. Coolify accepts a
plain `docker-compose.yml`, so deployment is: new Resource → Docker Compose →
point it at this repo.

```sh
docker compose up -d --build
```

- No host port is published. The container only `expose`s 50052 on the
  project network, so Traefik reaches it and the internet does not.
- Plain HTTP means Coolify's generated Traefik labels work unmodified:
  re-enable "Generate default labels" in Coolify and set the domain on port
  50052.
- For remote/Coolify deployments, set `RADIO_MCP_AUTH_TOKEN` (Coolify env
  vars) so the mutating tools require a Bearer token (see HTTP auth above).
- The image installs runtime deps only (`uv sync --frozen --no-dev`).
- The server itself speaks plaintext HTTP. TLS is terminated by Traefik; do
  not publish port 50052 to the internet directly.
- `/health` is the liveness probe (Coolify healthcheck and Docker
  `HEALTHCHECK`); it never touches the upstream Radio Browser API, so an
  upstream outage does not restart a healthy container.

To deploy on Coolify:

1. Set `RADIO_MCP_AUTH_TOKEN` in the resource's env vars —
   without the token the mutating tools are open to anyone who can reach the
   endpoint.
2. Re-enable "Generate default labels" and set the domain on port 50052.

Then test:

```sh
curl -sf https://<domain>/health
```

and connect an MCP client to `https://<domain>/mcp`.

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
