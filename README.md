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
docker network create coolify   # one-time, local only (see note below)
docker compose up -d --build
```

The compose file joins the `coolify` proxy network so Traefik can reach the
container; declaring it external means a local run needs that network to
exist, hence the one-time `docker network create`. On the server Coolify
already provides it.

- No host port is published. The container only `expose`s 50051 on the
  proxy network, so Traefik reaches it and the internet does not.
- For remote/Coolify deployments, set `RADIO_MCP_AUTH_TOKEN` (Coolify env
  vars) so the mutating tools require a Bearer token (see gRPC auth above).
- The image installs runtime deps only (`uv sync --frozen --no-dev`) and
  ships pre-generated protobuf stubs, so no build tools are needed at deploy.
- The server itself speaks plaintext gRPC. TLS is terminated by Traefik; do
  not publish port 50051 to the internet directly.

### Traefik / h2c (required)

gRPC is HTTP/2. Traefik's default backend scheme is `http`, which downgrades
the connection to HTTP/1.1 and makes every gRPC call fail while the container
still reports healthy. `docker-compose.yml` ships explicit Traefik labels that
set `loadbalancer.server.scheme=h2c`.

Three things about those labels are easy to get wrong:

- **`MCP_DOMAIN` needs Coolify's label escaping turned off.** By default
  Coolify rewrites `$` to `$$`, which delivers `${MCP_DOMAIN}` to the container
  as a literal string; Traefik then rejects the router outright
  (`is not a valid hostname`) and repeatedly fails ACME orders for it. Uncheck
  **"Escape special characters in labels"** in the resource's **Container
  Labels** section, and set `MCP_DOMAIN` in its env vars.
- **The container must join the `coolify` network.** `traefik.docker.network`
  only tells Traefik which network to read the backend IP from — it does not
  attach the container. Coolify attaches the proxy network on its own only
  when a domain is set in its UI, which this setup deliberately leaves empty.
- **Use a dedicated subdomain**, e.g. `radiobrowser-api-mcp.d.imaxim.org` —
  not the hostname that serves the Coolify dashboard, since two routers on one
  host compete. Traefik issues the certificate via `letsencrypt`.

To deploy on Coolify:

1. Set `MCP_DOMAIN` and `RADIO_MCP_AUTH_TOKEN` in the resource's env vars —
   without the token the mutating tools are open to anyone who can reach the
   endpoint.
2. In the **Container Labels** section, uncheck "Escape special characters in
   labels" so `MCP_DOMAIN` interpolates.

   Coolify's generated labels can stay on: they create a competing router for
   the same host using the default `http` scheme, but this router carries
   `priority=1000` and wins. Traefik otherwise ranks by rule length, and the
   generated `Host(x) && PathPrefix(/)` is longer than `Host(x)`.
3. Deploy, then confirm the rule label resolved on the server:

   ```sh
   docker inspect <container> --format \
     '{{index .Config.Labels "traefik.http.routers.radiobrowser-mcp.rule"}}'
   ```

   It must print the real hostname, not a literal `${MCP_DOMAIN}`.

Then test with a gRPC client over TLS on 443, no port suffix:

```sh
grpcurl radiobrowser-api-mcp.d.imaxim.org:443 radiomcp.RadioMcpService/ListTools
```

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
