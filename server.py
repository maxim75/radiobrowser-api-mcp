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
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent import futures as concurrent_futures
from dataclasses import asdict, dataclass

try:  # MCP SDK v2.x: FastMCP was renamed to MCPServer
    from mcp.server.mcpserver import MCPServer as _Server
    from mcp.server.mcpserver.exceptions import ToolError

    mcp = _Server("radio-now-playing")
except ImportError:  # MCP SDK v1.x
    from mcp.server.fastmcp import FastMCP as _Server

    try:
        from mcp.server.fastmcp.exceptions import ToolError
    except ImportError:  # very old v1: surface messages via RuntimeError

        class ToolError(RuntimeError):
            pass

    mcp = _Server("radio-now-playing")

RADIO_BROWSER_HOSTS = [
    "https://de1.api.radio-browser.info",
    "https://de2.api.radio-browser.info",
    "https://nl1.api.radio-browser.info",
]
USER_AGENT = "radio-now-playing-mcp/0.1.0 (contact: mcp-client)"
MAX_RESPONSE_BYTES = 10_000_000  # cap on upstream JSON bodies
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-"
    r"[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)


# --- Radio Browser API client (shared by the directory tools) ---

STATION_ORDERS = {
    "name",
    "url",
    "homepage",
    "favicon",
    "tags",
    "country",
    "state",
    "language",
    "votes",
    "codec",
    "bitrate",
    "lastcheckok",
    "lastchecktime",
    "clicktimestamp",
    "clickcount",
    "clicktrend",
    "changetimestamp",
    "random",
}
FACET_ORDERS = {"name", "stationcount"}


def _quote(value: str) -> str:
    """URL-quote a path segment; '/' must be encoded too (tag 'rock/pop')."""
    return urllib.parse.quote(value, safe="")


def _require_uuid(station_uuid: str) -> str:
    """Fail fast on malformed station UUIDs instead of a network round trip."""
    if not UUID_RE.match(station_uuid or ""):
        raise ToolError(f"invalid station uuid {station_uuid!r}")
    return station_uuid


def _to_int(value, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _api_get(path: str, params: dict | None = None, timeout: int = 15):
    """GET a Radio Browser JSON endpoint, failing over across mirrors.

    4xx responses fail fast (same bad request on every mirror); only 5xx and
    network errors trigger failover.
    """
    query = ("?" + urllib.parse.urlencode(params)) if params else ""
    last_error: Exception | None = None
    for host in RADIO_BROWSER_HOSTS:
        try:
            req = urllib.request.Request(
                f"{host}/{path}{query}", headers={"User-Agent": USER_AGENT}
            )
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                return json.loads(resp.read(MAX_RESPONSE_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            # Read a snippet for diagnostics before the socket closes.
            try:
                detail = exc.read(500).decode("utf-8", "replace")
            except Exception:
                detail = ""
            if 400 <= exc.code < 500:
                raise ToolError(
                    f"Radio Browser request failed (/{path}): HTTP {exc.code}: {detail}"
                )
            last_error = exc  # 5xx: try next mirror
            continue
        except Exception as exc:  # network error: try next mirror
            last_error = exc
            continue
    raise ToolError(f"Radio Browser request failed (/{path}): {last_error}")


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
                return json.loads(resp.read(MAX_RESPONSE_BYTES).decode("utf-8"))
        except urllib.error.HTTPError as exc:
            try:
                detail = exc.read(500).decode("utf-8", "replace")
            except Exception:
                detail = ""
            if 400 <= exc.code < 500:
                raise ToolError(
                    f"Radio Browser request failed (/{path}): HTTP {exc.code}: {detail}"
                )
            last_error = exc  # 5xx: try next mirror
            continue
        except Exception as exc:  # network error: try next mirror
            last_error = exc
            continue
    raise ToolError(f"Radio Browser request failed (/{path}): {last_error}")


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
        raise ToolError(
            f"invalid order {order!r}; choose from {sorted(STATION_ORDERS)}"
        )
    return {
        "order": order,
        "reverse": "true" if reverse else "false",
        "offset": max(0, int(offset)),
        "limit": max(1, min(int(limit), 200)),
        "hidebroken": "true" if hidebroken else "false",
    }


def _facet_params(order: str = "name", reverse: bool = False) -> dict:
    if order not in FACET_ORDERS:
        raise ToolError(f"invalid order {order!r}; choose from {sorted(FACET_ORDERS)}")
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


def _station_tokens(name: str) -> set[str]:
    """Normalize a station name for same-station comparison.

    Strips parenthesized qualifiers ("(320k)", "(SMOOTH953)") and splits on
    non-alphanumerics, so "smooth 95.3 Sydney (SMOOTH953)" and
    "Smooth FM 95.3 - Sydney" share {smooth, 95.3, sydney} = 3 tokens.
    The >= 2 overlap rule in get_now_playing means Sydney
    ({smooth, 953, 95.3, sydney}) vs Melbourne ({smooth, 915, 91.5,
    melbourne}) share only {smooth} = 1 and are correctly treated as
    DIFFERENT stations — never mix their tracks.
    """
    base = re.sub(r"\([^)]*\)", " ", name.lower())
    return set(re.findall(r"[a-z0-9.]+", base))


def known_station_fallback(query: str) -> list[StationCandidate]:
    """Return curated candidates when the query mentions a known station.

    Ranked by keyword overlap with a minimum of 2 matching tokens, so
    "smooth 91.5 melbourne" matches Melbourne (3) above Sydney (1, dropped).
    The >= 2 threshold also subsumes the old "smooth"-must-be-present guard:
    a bare "sydney" query scores 1 and matches nothing.
    """
    tokens = set(re.findall(r"[a-z0-9.]+", query.lower()))
    scored = [
        (len(tokens & set(keywords)), candidate)
        for keywords, candidate in KNOWN_STATIONS
        if len(tokens & set(keywords)) >= 2
    ]
    return [c for _, c in sorted(scored, key=lambda p: -p[0])]


def ranked_search(
    query: str, limit: int = 8, timeout: int = 15
) -> list[StationCandidate]:
    """Directory search with token-fallback + fuzzy ranking + curated entries.

    Token sub-queries run concurrently; each has its own `timeout` budget.
    """
    tokens = [t for t in re.findall(r"[a-z0-9.]+", query.lower()) if len(t) >= 3]
    queries = [query, *[t for t in tokens if t != query.lower()]]
    seen: dict[str, StationCandidate] = {}
    with concurrent_futures.ThreadPoolExecutor(
        max_workers=min(len(queries), 6)
    ) as pool:
        future_map = {
            pool.submit(search_stations, q, limit, timeout): q for q in queries
        }
        for future in concurrent_futures.as_completed(future_map):
            try:
                for c in future.result():
                    seen.setdefault(c.url, c)
            except ToolError:
                # ToolError is NOT a RuntimeError subclass under SDK v2, so it
                # needs its own clause: one bad batch must not kill the search.
                continue
    query_tokens = set(tokens)
    directory = list(seen.values())

    def score(c: StationCandidate) -> tuple[int, int, int]:
        overlap = len(query_tokens & _station_tokens(c.name))
        # Direct streams first (HLS carries no ICY metadata); note the list
        # sorts reverse=True, so non-HLS must score HIGHER (1, not 0).
        return (overlap, c.votes + c.clickcount, 1 if not c.hls else 0)

    directory.sort(key=score, reverse=True)
    # Keep only decent matches when the query was specific; otherwise the
    # top fuzzy hit (e.g. "SmoothFM" Portugal for a Sydney query) misleads.
    if query_tokens and directory and score(directory[0])[0] == 0:
        directory = []
    # Curated entries first: they are exact, verified streams, while directory
    # fuzzy hits (e.g. US "Smooth Jazz" for a Sydney query) often mislead.
    return known_station_fallback(query) + directory


def search_stations(
    query: str, limit: int = 8, timeout: int = 15
) -> list[StationCandidate]:
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
            bitrate=_to_int(item.get("bitrate")),
            country=item.get("country", ""),
            votes=_to_int(item.get("votes")),
            clickcount=_to_int(item.get("clickcount")),
            hls=bool(item.get("hls")),
        )
        for item in payload
        if item.get("url_resolved") or item.get("url")
    ]
    # Prefer direct (non-HLS) streams; HLS playlists don't carry ICY metadata.
    candidates.sort(key=lambda c: (c.hls, -(c.votes + c.clickcount)))
    return candidates


def _parse_stream_title(block: bytes) -> str | None:
    """Extract the first non-empty StreamTitle from an ICY metadata block.

    Blocks hold ';'-separated key='value' pairs; a song title may itself
    contain "';" (e.g. "Don't Stop'; ..."), so match the opening
    "StreamTitle='" and read to the terminating "';". An empty first pair
    (common during ad breaks: "StreamTitle='';StreamTitle='Real';") is
    skipped in favour of the next pair.
    """
    marker = b"StreamTitle='"
    pos = 0
    while True:
        start = block.find(marker, pos)
        if start == -1:
            return None
        start += len(marker)
        end = block.find(b"';", start)
        if end == -1:
            return None
        title = block[start:end].decode("utf-8", "replace").strip().strip("\x00")
        if title:
            return title
        pos = end + 2  # empty pair: keep scanning the rest of the block


def fetch_icy_metadata(
    stream_url: str, timeout: int = 20
) -> tuple[dict[str, str], str | None]:
    """Return (icy_headers, raw StreamTitle or None) from a live stream.

    Scans interleaved ICY metadata blocks (first non-empty StreamTitle wins)
    under an overall `timeout` deadline — a slow-but-alive stream must not
    stall the caller. Raises on connection errors or streams without ICY.
    """
    deadline = time.monotonic() + max(1, timeout)
    req = urllib.request.Request(
        stream_url,
        headers={"User-Agent": USER_AGENT, "Icy-MetaData": "1"},
    )
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        headers = {k.lower(): v for k, v in resp.headers.items()}
        metaint_raw = headers.get("icy-metaint")
        metaint = _to_int(metaint_raw, default=-1) if metaint_raw else -1
        if metaint <= 0:
            raise ToolError(
                f"stream does not advertise usable ICY metadata (icy-metaint={metaint_raw!r})"
            )

        icy_info = {
            k: headers[k]
            for k in ("icy-name", "icy-genre", "icy-br", "icy-url", "content-type")
            if k in headers
        }
        # Bitrate-scaled scan window: ad breaks carry empty StreamTitles, so a
        # fixed small block count often misses the song. Capped at 150 blocks.
        bitrate_kbps = max(_to_int(headers.get("icy-br"), default=0), 32)
        blocks = max(15, min(150, int(timeout * bitrate_kbps * 1000 / 8 / metaint) + 2))
        for _ in range(blocks):
            if time.monotonic() >= deadline:
                break
            chunk = resp.read(metaint)
            if len(chunk) < metaint:
                break  # stream ended
            length_byte = resp.read(1)
            if not length_byte:
                break
            meta_len = length_byte[0] * 16
            if meta_len == 0:
                continue  # no metadata in this interval
            title = _parse_stream_title(resp.read(meta_len))
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
        timeout: Seconds for directory + stream reads; clamped to 1-60, and
            shared as an overall deadline across all candidates tried.

    Returns:
        Dict with artist, title, raw_title, station_matched, country and
        stream_url (track fields null when the station is reachable but
        sending no track titles right now). Raises (as an MCP error) when
        nothing is found.
    """
    timeout = max(1, min(int(timeout), 60))
    candidates = ranked_search(station_name, timeout=timeout)
    if not candidates:
        raise ToolError(f"No stations found for {station_name!r}")

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

    errors: list[str] = []
    first_connected: dict | None = None
    first_tokens: set[str] = set()
    deadline = time.monotonic() + timeout
    for candidate in candidates:
        if time.monotonic() >= deadline:
            errors.append("overall time budget exhausted")
            break
        if not candidate.url.lower().startswith(("http://", "https://")):
            errors.append(f"{candidate.name}: unsupported URL {candidate.url!r}")
            continue
        try:
            # Per-fetch budget is whatever remains of the overall deadline.
            remaining = max(1, int(deadline - time.monotonic()))
            _icy_info, raw_title = fetch_icy_metadata(candidate.url, timeout=remaining)
        except (OSError, urllib.error.URLError, ToolError) as exc:
            # OSError covers socket errors; URLError wraps DNS/refused/timeouts
            # (socket.timeout == TimeoutError is an OSError subclass, caught too).
            errors.append(f"{candidate.name}: {exc}")
            continue
        if first_connected is not None and (
            len(_station_tokens(candidate.name) & first_tokens) < 2
        ):
            # A station connected but was silent (ad/talk break); this is a
            # DIFFERENT station — never answer with its song.
            break
        if raw_title:
            return result_for(candidate, raw_title)
        if first_connected is None:
            # Top-ranked station reached but silent. Remember it and only keep
            # trying entries that look like the SAME station (e.g. alternate
            # bitrates, which share >= 2 name tokens).
            first_connected = result_for(candidate, None)
            first_tokens = _station_tokens(candidate.name)
            continue
    if first_connected is not None:
        return first_connected

    tried = "; ".join(errors) if errors else "no usable streams"
    raise ToolError(f"Could not read now-playing info for {station_name!r}: {tried}")


@mcp.tool()
def search_radio_stations(query: str, limit: int = 8) -> list[dict]:
    """Search the Radio Browser directory for stations matching a name.

    Args:
        query: Free-text station name, e.g. "smooth sydney".
        limit: Max results; clamped to 1-20.

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
        raise ToolError(
            "pass at least one of name/country/countrycode/state/language/tag/codec"
        )
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
    payload = _api_get(
        f"json/stations/byuuid/{_quote(_require_uuid(station_uuid))}", None, timeout
    )
    if not payload:
        raise ToolError(f"no station found for uuid {station_uuid!r}")
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
    payload = _api_get(f"json/stations/{endpoint}/{_quote(value)}", params, timeout)
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
    return _stations_by_facet(
        "name", value, exact, order, reverse, offset, limit, hidebroken, timeout
    )


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
    return _stations_by_facet(
        "country", value, exact, order, reverse, offset, limit, hidebroken, timeout
    )


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
        f"json/stations/bycountrycodeexact/{_quote(value)}", params, timeout
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
    return _stations_by_facet(
        "state", value, exact, order, reverse, offset, limit, hidebroken, timeout
    )


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
    return _stations_by_facet(
        "language", value, exact, order, reverse, offset, limit, hidebroken, timeout
    )


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
    return _stations_by_facet(
        "tag", value, exact, order, reverse, offset, limit, hidebroken, timeout
    )


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
    return _stations_by_facet(
        "codec", value, exact, order, reverse, offset, limit, hidebroken, timeout
    )


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
    station_uuid: str, seconds: int = 0, timeout: int = 15
) -> list[dict]:
    """Automated stream-check history for a station (GET /json/checks).

    Args:
        station_uuid: Station uuid.
        seconds: Only checks newer than this many seconds. The API ignores
            `limit` here (verified live), so use `seconds` to bound the
            response — e.g. 86400 for the last day. Defaults to 0, which
            returns the FULL history (can be thousands of entries).
    """
    _require_uuid(station_uuid)
    params = {"stationuuid": station_uuid}
    if _to_int(seconds) > 0:
        params["seconds"] = _to_int(seconds)
    return _api_get("json/checks", params, timeout) or []


@mcp.tool()
def register_station_click(station_uuid: str, timeout: int = 15) -> dict:
    """Register a click (listen) for a station (GET /json/click/{uuid}).

    NOTE: increments the station's public click counter. Returns the station record.
    """
    return _api_get(f"json/click/{_quote(_require_uuid(station_uuid))}", None, timeout)


@mcp.tool()
def vote_for_station(station_uuid: str, timeout: int = 15) -> dict:
    """Vote for a station (GET /json/vote/{uuid}). NOTE: increments the public vote counter."""
    return _api_get(f"json/vote/{_quote(_require_uuid(station_uuid))}", None, timeout)


@mcp.tool()
def resolve_station_stream_url(station_uuid: str, timeout: int = 15) -> dict:
    """Get the current stream URL for a station (GET /json/url/{uuid}). Also counts as a click."""
    return _api_get(f"json/url/{_quote(_require_uuid(station_uuid))}", None, timeout)


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
        raise ToolError("name and url are required")
    payload = _api_post(
        "json/add",
        {
            key: value
            for key, value in {
                "name": name,
                "url": url,
                "homepage": homepage,
                "favicon": favicon,
                "country": country,
                "state": state,
                "language": language,
                "tags": tags,
            }.items()
            if value  # omit empty optionals; junk empties pollute the directory
        },
        timeout,
    )
    if not payload.get("ok"):
        raise ToolError(f"add_station rejected: {payload}")
    return payload


@mcp.tool()
def list_countries(
    order: str = "name", reverse: bool = False, timeout: int = 15
) -> list[dict]:
    """Countries in the directory with station counts (GET /json/countries)."""
    return _api_get("json/countries", _facet_params(order, reverse), timeout)


@mcp.tool()
def list_country_codes(timeout: int = 15) -> list[dict]:
    """ISO 3166-1 country codes in the directory with station counts (GET /json/countrycodes)."""
    return _api_get("json/countrycodes", None, timeout)


@mcp.tool()
def list_codecs(filter: str = "", timeout: int = 15) -> list[dict]:
    """Stream codecs with station counts (GET /json/codecs[/{filter}])."""
    path = f"json/codecs/{_quote(filter)}" if filter else "json/codecs"
    return _api_get(path, None, timeout)


@mcp.tool()
def list_states(country: str = "", timeout: int = 15) -> list[dict]:
    """States/regions with station counts (GET /json/states[/{country}]).

    Country must be the exact directory name, e.g. "Australia".
    """
    path = f"json/states/{_quote(country)}" if country else "json/states"
    return _api_get(path, None, timeout)


@mcp.tool()
def list_languages(filter: str = "", timeout: int = 15) -> list[dict]:
    """Broadcast languages with station counts (GET /json/languages[/{filter}])."""
    path = f"json/languages/{_quote(filter)}" if filter else "json/languages"
    return _api_get(path, None, timeout)


@mcp.tool()
def list_tags(filter: str = "", timeout: int = 15) -> list[dict]:
    """Tags/genres with station counts (GET /json/tags[/{filter}])."""
    path = f"json/tags/{_quote(filter)}" if filter else "json/tags"
    return _api_get(path, None, timeout)


@mcp.tool()
def get_directory_stats(timeout: int = 15) -> dict:
    """Directory server statistics (GET /json/stats): station counts, checks, clicks, etc."""
    return _api_get("json/stats", None, timeout)


@mcp.tool()
def list_directory_servers(timeout: int = 15) -> list[dict]:
    """Known directory mirror servers (GET /json/servers)."""
    return _api_get("json/servers", None, timeout)


def main(argv: list[str] | None = None) -> None:
    import argparse

    parser = argparse.ArgumentParser(description="radiobrowser-api-mcp server")
    parser.add_argument(
        "--transport",
        choices=["stdio", "grpc", "streamable-http"],
        default="stdio",
        help="stdio for local MCP clients, grpc or streamable-http for remote connections",
    )
    parser.add_argument("--host", default="127.0.0.1", help="listen host")
    parser.add_argument("--port", type=int, default=50051, help="gRPC listen port")
    parser.add_argument(
        "--http-port", type=int, default=8000, help="Streamable HTTP listen port"
    )
    parser.add_argument(
        "--path", default="/mcp", help="Streamable HTTP endpoint path"
    )
    args = parser.parse_args(argv)
    if args.transport == "grpc":
        from grpc_server import serve

        serve(args.host, args.port)
    elif args.transport == "streamable-http":
        mcp.run(
            transport="streamable-http",
            host=args.host,
            port=args.http_port,
            streamable_http_path=args.path,
        )
    else:
        mcp.run()


if __name__ == "__main__":
    main()
