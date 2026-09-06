"""Network-free unit tests for the opus_5 review fixes.

- Sydney/Melbourne token separation (get_now_playing invariant)
- Curated fallback ranking (#2)
- Same-station loop gate: silent station + different loud station (#3)
- HLS deprioritized in ranked_search (#4)
- StreamTitle parsing with "';" inside titles
- Error surfacing: ToolError messages survive the MCP wrapper (#1)
"""

from __future__ import annotations

import anyio
import pytest

import server
from server import StationCandidate


def _cand(name: str, url: str, hls: bool = False) -> StationCandidate:
    return StationCandidate(
        name=name,
        url=url,
        codec="MP3",
        bitrate=128,
        country="X",
        votes=0,
        clickcount=0,
        hls=hls,
    )


# --- _station_tokens: Sydney vs Melbourne separation ---


def test_sydney_melbourne_are_different_stations():
    syd = server._station_tokens("smooth 95.3 Sydney (SMOOTH953)")
    mel = server._station_tokens("smooth 91.5 Melbourne (SMOOTH915)")
    assert len(syd & mel) < 2  # only {"smooth"} -> never mix tracks


def test_curated_matches_directory_naming():
    curated = server._station_tokens("smooth 95.3 Sydney (SMOOTH953)")
    directory = server._station_tokens("Smooth FM 95.3 - Sydney - 95.3 FM")
    assert len(curated & directory) >= 2  # same station, alternate bitrates


# --- known_station_fallback ranking (#2) ---


def test_melbourne_query_returns_melbourne_first():
    matches = server.known_station_fallback("smooth 91.5 melbourne")
    assert [c.name for c in matches] == ["smooth 91.5 Melbourne (SMOOTH915)"]


def test_sydney_query_returns_sydney_only():
    matches = server.known_station_fallback("Smooth FM Sydney")
    assert [c.name for c in matches] == ["smooth 95.3 Sydney (SMOOTH953)"]


def test_bare_city_does_not_match():
    assert server.known_station_fallback("Sydney talk radio") == []
    assert server.known_station_fallback("smooth") == []


# --- get_now_playing loop gate (#3): stub fetch_icy_metadata ---


def test_silent_station_then_different_loud_station_returns_null(monkeypatch):
    sydney = _cand("smooth 95.3 Sydney (SMOOTH953)", "http://sydney/stream")
    melbourne = _cand("smooth 91.5 Melbourne (SMOOTH915)", "http://melbourne/stream")

    def fake_fetch(url, timeout=20):
        return {}, "Artist B - Song B" if "melbourne" in url else None

    monkeypatch.setattr(server, "ranked_search", lambda *a, **k: [sydney, melbourne])
    monkeypatch.setattr(server, "fetch_icy_metadata", fake_fetch)
    # The @mcp.tool() decorator passes the plain function through.
    result = server.get_now_playing("smooth sydney", timeout=5)
    assert result["station_matched"] == sydney.name
    assert result["artist"] is None and result["title"] is None


def test_same_station_alternate_bitrate_title_is_returned(monkeypatch):
    main = _cand("smooth 95.3 Sydney (SMOOTH953)", "http://sydney/128")
    alt = _cand("Smooth FM 95.3 - Sydney (AAC+ 320k)", "http://sydney/320")

    def fake_fetch(url, timeout=20):
        return {}, "Artist A - Song A" if url.endswith("/320") else None

    monkeypatch.setattr(server, "ranked_search", lambda *a, **k: [main, alt])
    monkeypatch.setattr(server, "fetch_icy_metadata", fake_fetch)
    result = server.get_now_playing("smooth sydney", timeout=5)
    assert result["artist"] == "Artist A"
    assert result["station_matched"] == alt.name


# --- HLS deprioritized (#4) ---


def test_direct_stream_ranks_above_hls_on_tie(monkeypatch):
    direct = _cand("Jazz Radio A", "http://a/stream", hls=False)
    hls = _cand("Jazz Radio B", "http://b/stream", hls=True)

    monkeypatch.setattr(server, "search_stations", lambda *a, **k: [hls, direct])
    monkeypatch.setattr(server, "known_station_fallback", lambda q: [])
    ordered = server.ranked_search("jazz radio")
    assert [c.name for c in ordered] == ["Jazz Radio A", "Jazz Radio B"]


# --- StreamTitle parsing ---


@pytest.mark.parametrize(
    ("block", "expected"),
    [
        (b"StreamTitle='Artist - Song';", "Artist - Song"),
        (b"StreamTitle='Don\\'t Stop';StreamTitle='X';", "Don\\'t Stop"),
        (b"StreamTitle='';", None),
        (b"StreamTitle='';StreamTitle='Real Song';", "Real Song"),
        (b"StreamTitle='Ad - break';StreamUrl='http://x';", "Ad - break"),
        (b"no metadata here", None),
        (b"StreamTitle='unterminated", None),
    ],
)
def test_parse_stream_title(block, expected):
    assert server._parse_stream_title(block) == expected


# --- Error surfacing (#1): ToolError messages reach the client ---


def test_tool_error_message_survives_mcp_wrapper():
    async def go():
        try:
            await server.mcp.call_tool("get_station_by_uuid", {"station_uuid": "nope"})
            raise AssertionError("expected tool call to fail")
        except Exception as exc:
            assert "invalid station uuid" in str(exc.__cause__ or exc)

    anyio.run(go)
