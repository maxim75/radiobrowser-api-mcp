"""MCP server for the Radio Browser directory (api.radio-browser.info)
plus live "now playing" track info via ICY/Shoutcast stream metadata.

Tools:
    get_now_playing(...)       -> live track for a station (stream metadata)
    search_radio_stations(...) -> fuzzy directory search (feeds now-playing)
    advanced_station_search / list_all_stations / get_station_by_uuid /
    find_stations_by_* / get_top_voted_* / get_most_clicked_* /
    get_recently_{clicked,updated}_stations / get_station_check_history /
    register_station_click / vote_for_station / resolve_station_stream_url /
    add_station                    -> mirrors of the Radio Browser webservice
    list_{countries,country_codes,codecs,states,languages,tags} /
    get_directory_stats / list_directory_servers

Run:
    uv run server.py                       # stdio transport (for MCP clients)
    uv run server.py --transport grpc      # gRPC mode for remote connections
    uv run server.py --transport grpc --host 0.0.0.0 --port 50051
"""

from __future__ import annotations

import json
import re
import socket
import urllib.parse
import urllib.request
from dataclasses import asdict, dataclass

try:  # MCP SDK v2.x: FastMCP was renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server

    mcp = _Server("radio-now-playing")
except ImportError:  # MCP SDK v1.x
    from mcp.server.fastmcp import FastMCP as _Server

    mcp = _Server("radio-now-playing")

RADIO_BROWSER_HOSTS = [
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
]
USER_AGENT = "radio-now-playing-mcp/0.1.0 (contact: mcp-client)"
STREAM_TITLE_RE = re.compile(rb"StreamTitle='(.*?)';", re.DOTALL)


# --- Radio Browser API client (shared by the directory tools) ---

STATION_ORDERS = {
    "name", "url", "homepage", "favicon", "tags", "country", "state",
    "language", "votes", "codec", "bitrate", "lastcheckok", "lastchecktime",
    "clicktimestamp", "clickcount", "clicktrend", "changetimestamp", "random",
}
FACET_ORDERS = {"name", "stationcount"}


def _api_get(path: str, params: dict | None = None, timeout: int = 15):
    """GET a Radio Browser JSON endpoint, failing over across mirrors."""
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    last_error: Exception | None = None
    for host in RADIO_BROWSER_HOSTS:
        try:
            req = urllib.request.Request(
                f"{host}/{path}{query}", headers={"User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # try next mirror
            last_error = exc
            continue
    raise RuntimeError(f"Radio Browser request failed (/{path}): {last_error}")


def _api_post(path: str, data: dict, timeout: int = 15) -> dict:
    """POST form data to a Radio Browser JSON endpoint, with mirror failover."""
    body = urllib.parse.urlencode(data).encode("utf-8")
    last_error: Exception | None = None
    for host in RADIO_BROWSER_HOSTS:
        try:
            req = urllib.request.Request(
                f"{host}/{path}", data=body, headers={"User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except Exception as exc:  # try next mirror
            last_error = exc
            continue
    raise RuntimeError(f"Radio Browser request failed (/{path}): {last_error}")


def _slim_station(item: dict) -> dict:
    """Trim a raw station object to the fields MCP clients actually need."""
    return {
        "stationuuid": item.get("stationuuid"),
        "name": item.get("name"),
        "url": item.get("url_resolved") or item.get("url"),
        "homepage": item.get("homepage"),
        "favicon": item.get("favicon"),
        "tags": item.get("tags"),
        "country": item.get("country"),
        "countrycode": item.get("countrycode"),
        "state": item.get("state"),
        "language": item.get("language"),
        "languagecodes": item.get("languagecodes"),
        "votes": item.get("votes"),
        "clickcount": item.get("clickcount"),
        "codec": item.get("codec"),
        "bitrate": item.get("bitrate"),
        "hls": bool(item.get("hls")),
        "lastcheckok": bool(item.get("lastcheckok")),
    }


def _slim_stations(payload) -> list[dict]:
    return [_slim_station(item) for item in payload or []]


def _station_list_params(
    order: str = "votes",
    reverse: bool = True,
    offset: int = 0,
    limit: int = 20,
    hidebroken: bool = True,
) -> dict:
    """Validate + build order/reverse/offset/limit/hidebroken query params."""
    if order not in STATION_ORDERS:
        raise ValueError(f"invalid order {order!r}; choose from {sorted(STATION_ORDERS)}")
    return {
        "order": order,
        "reverse": "true" if reverse else "false",
        "offset": max(0, int(offset)),
        "limit": max(1, min(int(limit), 200)),
        "hidebroken": "true" if hidebroken else "false",
    }


def _facet_params(order: str = "name", reverse: bool = False) -> dict:
    if order not in FACET_ORDERS:
        raise ValueError(f"invalid order {order!r}; choose from {sorted(FACET_ORDERS)}")
    return {"order": order, "reverse": "true" if reverse else "false"}


@dataclass
class StationCandidate:
    name: str
    url: str
    codec: str
    bitrate: int
    country: str
    votes: int
    clickcount: int
    hls: bool


# Curated fallback streams for stations poorly covered by the directory.
# (StreamTheWorld redirect URLs; verified live for SMOOTH953.)
KNOWN_STATIONS: list[tuple[list[str], StationCandidate]] = [
    (
        ["smooth", "sydney", "953", "95.3"],
        StationCandidate(
            name="smooth 95.3 Sydney (SMOOTH953)",
            url="http://playerservices.streamtheworld.com/api/livestream-redirect/SMOOTH953_AAC128.aac",
            codec="AAC+",
            bitrate=128,
            country="Australia",
            votes=0,
            clickcount=0,
            hls=False,
        ),
    ),
    (
        ["smooth", "melbourne", "915", "91.5"],
        StationCandidate(
            name="smooth 91.5 Melbourne (SMOOTH915)",
            url="http://playerservices.streamtheworld.com/api/livestream-redirect/SMOOTH915_AAC128.aac",
            codec="AAC+",
            bitrate=128,
            country="Australia",
            votes=0,
            clickcount=0,
            hls=False,
        ),
    ),
]


def known_station_fallback(query: str) -> list[StationCandidate]:
    """Return curated candidates when the query mentions a known station."""
    tokens = set(re.findall(r"[a-z0-9.]+", query.lower()))
    matches = [c for keywords, c in KNOWN_STATIONS if tokens & set(keywords)]
    # Only use the fallback when "smooth" itself is mentioned, to avoid
    # hijacking unrelated queries like "Sydney talk radio".
    if matches and "smooth" not in tokens:
        return []
    return matches


def ranked_search(query: str, limit: int = 8, timeout: int = 15) -> list[StationCandidate]:
    """Directory search with token-fallback + fuzzy ranking + curated entries."""
    seen: dict[str, StationCandidate] = {}
    queries = [query]
    tokens = [t for t in re.findall(r"[a-z0-9.]+", query.lower()) if len(t) >= 3]
    queries += tokens  # e.g. "Smooth FM Sydney" -> also try "smooth"
    for q in queries:
        try:
            for c in search_stations(q, limit=limit, timeout=timeout):
                seen.setdefault(c.url, c)
        except RuntimeError:
            continue
    query_tokens = set(tokens)
    directory = list(seen.values())

    def score(c: StationCandidate) -> tuple[int, int, int]:
        name_tokens = set(re.findall(r"[a-z0-9.]+", c.name.lower()))
        overlap = len(query_tokens & name_tokens)
        return (overlap, c.votes + c.clickcount, 0 if not c.hls else 1)

    directory.sort(key=score, reverse=True)
    # Keep only decent matches when the query was specific; otherwise the
    # top fuzzy hit (e.g. "SmoothFM" Portugal for a Sydney query) misleads.
    if query_tokens and directory and score(directory[0])[0] == 0:
        directory = []
    # Curated entries first: they are exact, verified streams, while directory
    # fuzzy hits (e.g. US "Smooth Jazz" for a Sydney query) often mislead.
    return known_station_fallback(query) + directory


def search_stations(query: str, limit: int = 8, timeout: int = 15) -> list[StationCandidate]:
    """Find streamable stations matching `query` via the Radio Browser API."""
    payload = _api_get(
        "json/stations/search",
        {
            "name": query,
            "limit": max(1, min(int(limit), 200)),
            "order": "votes",
            "reverse": "true",
            "hidebroken": "true",
        },
        timeout,
    )
    candidates = [
        StationCandidate(
            name=item.get("name", "unknown"),
            url=item.get("url_resolved") or item.get("url", ""),
            codec=item.get("codec", ""),
            bitrate=int(item.get("bitrate") or 0),
            country=item.get("country", ""),
            votes=int(item.get("votes") or 0),
            clickcount=int(item.get("clickcount") or 0),
            hls=bool(item.get("hls")),
        )
        for item in payload
        if item.get("url_resolved") or item.get("url")
    ]
    # Prefer direct (non-HLS) streams; HLS playlists don't carry ICY metadata.
    candidates.sort(key=lambda c: (c.hls, -(c.votes + c.clickcount)))
    return candidates


def fetch_icy_metadata(
    stream_url: str, timeout: int = 20
) -> tuple[dict[str, str], str | None]:
    """Return (icy_headers, raw StreamTitle or None) from a live stream.

    Reads interleaved ICY metadata blocks (first non-empty StreamTitle wins).
    Raises on connection errors or when the server sends no ICY metadata.
    """
    req = urllib.request.Request(
        stream_url,
        headers={"User-Agent": USER_AGENT, "Icy-MetaData": "1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        metaint_raw = headers.get("icy-metaint")
        if not metaint_raw:
            raise RuntimeError("stream does not advertise ICY metadata (no icy-metaint)")
        try:
            metaint = int(metaint_raw)
        except ValueError:
            raise RuntimeError(f"invalid icy-metaint header: {metaint_raw!r}")
        if metaint <= 0:
            raise RuntimeError(f"invalid icy-metaint header: {metaint_raw!r}")

        icy_info = {
            k: headers[k]
            for k in ("icy-name", "icy-genre", "icy-br", "icy-url", "content-type")
            if k in headers
        }
        # Scan up to ~`timeout` seconds of audio: ad breaks carry empty
        # StreamTitles, so a fixed small block count often misses the song.
        try:
            bitrate_kbps = max(int(headers.get("icy-br") or 0), 32)
        except ValueError:
            bitrate_kbps = 128
        blocks = max(15, min(150, int(timeout * bitrate_kbps * 1000 / 8 / metaint) + 2))
        for _ in range(blocks):
            chunk = resp.read(metaint)
            if len(chunk) < metaint:
                break  # stream ended
            length_byte = resp.read(1)
            if not length_byte:
                break
            meta_len = length_byte[0] * 16
            if meta_len == 0:
                continue  # no metadata in this interval
            block = resp.read(meta_len)
            match = STREAM_TITLE_RE.search(block)
            if match:
                title = match.group(1).decode("utf-8", "replace").strip().strip("\x00")
                if title:
                    return icy_info, title
        return icy_info, None


def split_artist_title(raw: str) -> tuple[str | None, str]:
    """Split 'Artist - Title' into parts; fall back to (None, raw)."""
    if " - " in raw:
        artist, title = raw.split(" - ", 1)
        return artist.strip() or None, title.strip()
    return None, raw


@mcp.tool()
def get_now_playing(station_name: str, timeout: int = 20) -> dict:
    """Get the track currently playing on a radio station.

    Args:
        station_name: Station name, e.g. "Smooth FM Sydney" or "smooth 95.3".
        timeout: Per-request timeout in seconds for directory + stream reads.

    Returns:
        Dict with artist, title, raw_title, station_matched, country and
        stream_url (track fields null when the station is reachable but
        sending no track titles right now). Raises (as an MCP error) when
        nothing is found.
    """
    candidates = ranked_search(station_name, timeout=timeout)
    if not candidates:
        raise RuntimeError(f"No stations found for {station_name!r}")

    def result_for(candidate: StationCandidate, raw_title: str | None) -> dict:
        artist, title = split_artist_title(raw_title) if raw_title else (None, None)
        return {
            "artist": artist,
            "title": title,
            "raw_title": raw_title,
            "station_matched": candidate.name,
            "country": candidate.country,
            "stream_url": candidate.url,
        }

    def name_tokens(name: str) -> set[str]:
        return set(re.findall(r"[a-z0-9.]+", name.lower()))

    errors: list[str] = []
    first_connected: dict | None = None
    first_tokens: set[str] = set()
    for candidate in candidates:
        if not candidate.url.lower().startswith(("http://", "https://")):
            errors.append(f"{candidate.name}: unsupported URL {candidate.url!r}")
            continue
        try:
            icy_info, raw_title = fetch_icy_metadata(candidate.url, timeout=timeout)
        except (OSError, socket.timeout, RuntimeError) as exc:
            errors.append(f"{candidate.name}: {exc}")
            continue
        if raw_title:
            return result_for(candidate, raw_title)
        if first_connected is None:
            # Top-ranked station reached but silent (ad/talk break). Only keep
            # trying entries that look like the SAME station (e.g. alternate
            # bitrates) — never answer with a different station's song.
            first_connected = result_for(candidate, None)
            first_tokens = name_tokens(candidate.name)
            continue
        if len(name_tokens(candidate.name) & first_tokens) >= 2:
            continue  # same station, same break — skip without extra waiting
        break
    if first_connected is not None:
        return first_connected

    tried = "; ".join(errors) if errors else "no usable streams"
    raise RuntimeError(f"Could not read now-playing info for {station_name!r}: {tried}")


@mcp.tool()
def search_radio_stations(query: str, limit: int = 8) -> list[dict]:
    """Search the Radio Browser directory for stations matching a name.

    Args:
        query: Free-text station name, e.g. "smooth sydney".
        limit: Max results (1-20).

    Returns:
        List of station dicts with name, stream URL, codec, bitrate, country.
    """
    limit = max(1, min(int(limit), 20))
    return [asdict(c) for c in search_stations(query, limit=limit)]


@mcp.tool()
def advanced_station_search(
    name: str = "",
    name_exact: bool = False,
    country: str = "",
    country_exact: bool = False,
    countrycode: str = "",
    state: str = "",
    state_exact: bool = False,
    language: str = "",
    language_exact: bool = False,
    tag: str = "",
    tag_exact: bool = False,
    codec: str = "",
    codec_exact: bool = False,
    order: str = "votes",
    reverse: bool = True,
    offset: int = 0,
    limit: int = 20,
    hidebroken: bool = True,
    timeout: int = 15,
) -> list[dict]:
    """Advanced station search with field filters (GET /json/stations/search).

    At least one of name/country/countrycode/state/language/tag/codec is required.
    The *_exact flags switch that field to exact matching.
    """
    params: dict = {}
    fields = {
        "name": (name, name_exact),
        "country": (country, country_exact),
        "state": (state, state_exact),
        "language": (language, language_exact),
        "tag": (tag, tag_exact),
        "codec": (codec, codec_exact),
    }
    for field, (value, exact) in fields.items():
        if value:
            params[field] = value
            if exact:
                params[field + "Exact"] = "true"
    if countrycode:
        params["countrycode"] = countrycode
    if not params:
        raise ValueError("pass at least one of name/country/countrycode/state/language/tag/codec")
    params.update(_station_list_params(order, reverse, offset, limit, hidebroken))
    return _slim_stations(_api_get("json/stations/search", params, timeout))


@mcp.tool()
def list_all_stations(
    order: str = "votes",
    reverse: bool = True,
    offset: int = 0,
    limit: int = 20,
    hidebroken: bool = True,
    timeout: int = 15,
) -> list[dict]:
    """List stations in the directory (GET /json/stations). Paginate with offset/limit."""
    params = _station_list_params(order, reverse, offset, limit, hidebroken)
    return _slim_stations(_api_get("json/stations", params, timeout))


@mcp.tool()
def get_station_by_uuid(station_uuid: str, timeout: int = 15) -> dict:
    """Get one station by its stationuuid (GET /json/stations/byuuid/{uuid})."""
    payload = _api_get(f"json/stations/byuuid/{urllib.parse.quote(station_uuid)}", None, timeout)
    if not payload:
        raise RuntimeError(f"no station found for uuid {station_uuid!r}")
    return _slim_station(payload[0])


_FACET_ENDPOINTS = {
    "name": ("byname", "bynameexact"),
    "country": ("bycountry", "bycountryexact"),
    "state": ("bystate", "bystateexact"),
    "language": ("bylanguage", "bylanguageexact"),
    "tag": ("bytag", "bytagexact"),
    "codec": ("bycodec", "bycodecexact"),
}


def _stations_by_facet(
    facet: str,
    value: str,
    exact: bool,
    order: str,
    reverse: bool,
    offset: int,
    limit: int,
    hidebroken: bool,
    timeout: int,
) -> list[dict]:
    base, exact_base = _FACET_ENDPOINTS[facet]
    endpoint = exact_base if exact else base
    params = _station_list_params(order, reverse, offset, limit, hidebroken)
    payload = _api_get(
        f"json/stations/{endpoint}/{urllib.parse.quote(value)}", params, timeout
    )
    return _slim_stations(payload)


@mcp.tool()
def find_stations_by_name(
    value: str,
    exact: bool = False,
    order: str = "votes",
    reverse: bool = True,
    offset: int = 0,
    limit: int = 20,
    hidebroken: bool = True,
    timeout: int = 15,
) -> list[dict]:
    """Find stations by name (GET /json/stations/byname[/exact]/{name})."""
    return _stations_by_facet("name", value, exact, order, reverse, offset, limit, hidebroken, timeout)


@mcp.tool()
def find_stations_by_country(
    value: str,
    exact: bool = False,
    order: str = "votes",
    reverse: bool = True,
    offset: int = 0,
    limit: int = 20,
    hidebroken: bool = True,
    timeout: int = 15,
) -> list[dict]:
    """Find stations by country, e.g. "Australia" (GET /json/stations/bycountry[/exact]/{country})."""
    return _stations_by_facet("country", value, exact, order, reverse, offset, limit, hidebroken, timeout)


@mcp.tool()
def find_stations_by_country_code(
    value: str,
    order: str = "votes",
    reverse: bool = True,
    offset: int = 0,
    limit: int = 20,
    hidebroken: bool = True,
    timeout: int = 15,
) -> list[dict]:
    """Find stations by ISO 3166-1 country code, e.g. "AU" (GET /json/stations/bycountrycodeexact/{code})."""
    params = _station_list_params(order, reverse, offset, limit, hidebroken)
    payload = _api_get(
        f"json/stations/bycountrycodeexact/{urllib.parse.quote(value)}", params, timeout
    )
    return _slim_stations(payload)


@mcp.tool()
def find_stations_by_state(
    value: str,
    exact: bool = False,
    order: str = "votes",
    reverse: bool = True,
    offset: int = 0,
    limit: int = 20,
    hidebroken: bool = True,
    timeout: int = 15,
) -> list[dict]:
    """Find stations by state/region (GET /json/stations/bystate[/exact]/{state})."""
    return _stations_by_facet("state", value, exact, order, reverse, offset, limit, hidebroken, timeout)


@mcp.tool()
def find_stations_by_language(
    value: str,
    exact: bool = False,
    order: str = "votes",
    reverse: bool = True,
    offset: int = 0,
    limit: int = 20,
    hidebroken: bool = True,
    timeout: int = 15,
) -> list[dict]:
    """Find stations by broadcast language, e.g. "english" (GET /json/stations/bylanguage[/exact]/{language})."""
    return _stations_by_facet("language", value, exact, order, reverse, offset, limit, hidebroken, timeout)


@mcp.tool()
def find_stations_by_tag(
    value: str,
    exact: bool = False,
    order: str = "votes",
    reverse: bool = True,
    offset: int = 0,
    limit: int = 20,
    hidebroken: bool = True,
    timeout: int = 15,
) -> list[dict]:
    """Find stations by tag/genre, e.g. "chill" (GET /json/stations/bytag[/exact]/{tag})."""
    return _stations_by_facet("tag", value, exact, order, reverse, offset, limit, hidebroken, timeout)


@mcp.tool()
def find_stations_by_codec(
    value: str,
    exact: bool = False,
    order: str = "votes",
    reverse: bool = True,
    offset: int = 0,
    limit: int = 20,
    hidebroken: bool = True,
    timeout: int = 15,
) -> list[dict]:
    """Find stations by stream codec, e.g. "MP3" (GET /json/stations/bycodec[/exact]/{codec})."""
    return _stations_by_facet("codec", value, exact, order, reverse, offset, limit, hidebroken, timeout)


@mcp.tool()
def get_top_voted_stations(count: int = 10, timeout: int = 15) -> list[dict]:
    """Highest-voted stations (GET /json/stations/topvote/{count})."""
    count = max(1, min(int(count), 100))
    return _slim_stations(_api_get(f"json/stations/topvote/{count}", None, timeout))


@mcp.tool()
def get_most_clicked_stations(count: int = 10, timeout: int = 15) -> list[dict]:
    """Most-clicked stations (GET /json/stations/topclick/{count})."""
    count = max(1, min(int(count), 100))
    return _slim_stations(_api_get(f"json/stations/topclick/{count}", None, timeout))


@mcp.tool()
def get_recently_clicked_stations(count: int = 10, timeout: int = 15) -> list[dict]:
    """Most recently clicked stations (GET /json/stations/lastclick/{count})."""
    count = max(1, min(int(count), 100))
    return _slim_stations(_api_get(f"json/stations/lastclick/{count}", None, timeout))


@mcp.tool()
def get_recently_updated_stations(count: int = 10, timeout: int = 15) -> list[dict]:
    """Most recently changed/added stations (GET /json/stations/lastchange/{count})."""
    count = max(1, min(int(count), 100))
    return _slim_stations(_api_get(f"json/stations/lastchange/{count}", None, timeout))


@mcp.tool()
def get_station_check_history(
    station_uuid: str, seconds: int = 0, limit: int = 10, timeout: int = 15
) -> list[dict]:
    """Automated stream-check history for a station (GET /json/checks?stationuuid=...).

    Args:
        station_uuid: Station uuid.
        seconds: Only checks newer than this many seconds (0 = all).
        limit: Max checks returned (newest first).
    """
    params = {"stationuuid": station_uuid}
    if int(seconds) > 0:
        params["seconds"] = int(seconds)
    payload = _api_get("json/checks", params, timeout) or []
    return payload[: max(1, int(limit))]


@mcp.tool()
def register_station_click(station_uuid: str, timeout: int = 15) -> dict:
    """Register a click (listen) for a station (GET /json/click/{uuid}).

    NOTE: increments the station's public click counter. Returns the station record.
    """
    return _api_get(f"json/click/{urllib.parse.quote(station_uuid)}", None, timeout)


@mcp.tool()
def vote_for_station(station_uuid: str, timeout: int = 15) -> dict:
    """Vote for a station (GET /json/vote/{uuid}). NOTE: increments the public vote counter."""
    return _api_get(f"json/vote/{urllib.parse.quote(station_uuid)}", None, timeout)


@mcp.tool()
def resolve_station_stream_url(station_uuid: str, timeout: int = 15) -> dict:
    """Get the current stream URL for a station (GET /json/url/{uuid}). Also counts as a click."""
    return _api_get(f"json/url/{urllib.parse.quote(station_uuid)}", None, timeout)


@mcp.tool()
def add_station(
    name: str,
    url: str,
    homepage: str = "",
    favicon: str = "",
    country: str = "",
    state: str = "",
    language: str = "",
    tags: str = "",
    timeout: int = 15,
) -> dict:
    """Submit a new station to the directory (POST /json/add). Only name + url are required."""
    if not name or not url:
        raise ValueError("name and url are required")
    payload = _api_post(
        "json/add",
        {
            "name": name,
            "url": url,
            "homepage": homepage,
            "favicon": favicon,
            "country": country,
            "state": state,
            "language": language,
            "tags": tags,
        },
        timeout,
    )
    if not payload.get("ok"):
        raise RuntimeError(f"add_station rejected: {payload}")
    return payload


@mcp.tool()
def list_countries(order: str = "name", reverse: bool = False, timeout: int = 15) -> list[dict]:
    """Countries in the directory with station counts (GET /json/countries)."""
    return _api_get("json/countries", _facet_params(order, reverse), timeout)


@mcp.tool()
def list_country_codes(timeout: int = 15) -> list[dict]:
    """ISO 3166-1 country codes in the directory with station counts (GET /json/countrycodes)."""
    return _api_get("json/countrycodes", None, timeout)


@mcp.tool()
def list_codecs(filter: str = "", timeout: int = 15) -> list[dict]:
    """Stream codecs with station counts (GET /json/codecs[/{filter}])."""
    path = f"json/codecs/{urllib.parse.quote(filter)}" if filter else "json/codecs"
    return _api_get(path, None, timeout)


@mcp.tool()
def list_states(country: str = "", timeout: int = 15) -> list[dict]:
    """States/regions with station counts (GET /json/states[/{country}]).

    Country must be the exact directory name, e.g. "Australia".
    """
    path = f"json/states/{urllib.parse.quote(country)}" if country else "json/states"
    return _api_get(path, None, timeout)


@mcp.tool()
def list_languages(filter: str = "", timeout: int = 15) -> list[dict]:
    """Broadcast languages with station counts (GET /json/languages[/{filter}])."""
    path = f"json/languages/{urllib.parse.quote(filter)}" if filter else "json/languages"
    return _api_get(path, None, timeout)


@mcp.tool()
def list_tags(filter: str = "", timeout: int = 15) -> list[dict]:
    """Tags/genres with station counts (GET /json/tags[/{filter}])."""
    path = f"json/tags/{urllib.parse.quote(filter)}" if filter else "json/tags"
    return _api_get(path, None, timeout)


@mcp.tool()
def get_directory_stats(timeout: int = 15) -> dict:
    """Directory server statistics (GET /json/stats): station counts, checks, clicks, etc."""
    return _api_get("json/stats", None, timeout)


@mcp.tool()
def list_directory_servers(timeout: int = 15) -> list[dict]:
    """Known directory mirror servers (GET /json/servers)."""
    return _api_get("json/servers", None, timeout)


if __name__ == "__main__":
    import argparse

    parser = argparse.ArgumentParser(description="radiobrowser-api-mcp server")
    parser.add_argument(
        "--transport", choices=["stdio", "grpc"], default="stdio",
        help="stdio for local MCP clients, grpc for remote connections",
    )
    parser.add_argument("--host", default="127.0.0.1", help="gRPC listen host")
    parser.add_argument("--port", type=int, default=50051, help="gRPC listen port")
    args = parser.parse_args()
    if args.transport == "grpc":
        from grpc_server import serve

        serve(args.host, args.port)
    else:
        mcp.run()
