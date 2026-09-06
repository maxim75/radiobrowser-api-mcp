# Code Review — `radiobrowser-api-mcp`

**Reviewer:** Claude Opus 5 · **Date:** 2026-09-06 · **Commit:** `750f745`
**Scope:** `server.py`, `grpc_server.py`, `radio_mcp.proto`, `Dockerfile`, `docker-compose.yml`, `pyproject.toml`, docs

---

## Summary

This is well-above-average code for its size: a clear module docstring, comments that
explain *why* rather than *what* (the ICY parsing rationale, the mirror-failover policy,
the "don't mix stations" invariant), consistent helper factoring (`_slim_station`,
`_station_list_params`, `_facet_params`), validation before network calls, and a
genuinely well-designed gRPC bridge that needs no changes when tools are added.

The findings below are mostly about the gap between what `AGENTS.md` and the docstrings
*promise* and what the code actually does. Every item marked **confirmed** was verified
by running it against the installed environment, not inferred by reading.

| # | Severity | Finding |
|---|----------|---------|
| 1 | High | All tool error messages are discarded before reaching the client |
| 2 | High | Curated fallback returns the wrong city's station |
| 3 | High | The same-station guard is unreachable for the case it exists to prevent |
| 4 | Medium | HLS sort is inverted in `ranked_search` |
| 5 | Medium | `get_now_playing` has no overall time budget |
| 6 | Medium | Auth token compared non-constant-time (+ SSRF surface) |
| 7 | Low | Nine smaller items (see below) |

---

## 1. High — all tool error messages are discarded before reaching the client

The MCP SDK v2 wraps any non-`ToolError` exception in `UnexpectedToolError`, whose message
is only `Error executing tool <name>`; the original is moved to `__cause__` and withheld
from the client. Every tool in `server.py` raises `RuntimeError` / `ValueError`.

**Confirmed:**

```
get_station_by_uuid: UnexpectedToolError: Error executing tool get_station_by_uuid
   | cause=ValueError: invalid station uuid 'not-a-uuid'
advanced_station_search: UnexpectedToolError: Error executing tool advanced_station_search
   | cause=ValueError: pass at least one of name/country/countrycode/state/language/tag/codec
list_all_stations: UnexpectedToolError: Error executing tool list_all_stations
   | cause=ValueError: invalid order 'bogus'; choose from ['bitrate', 'changetimestamp', ...]
```

So the carefully constructed diagnostics never reach the model or the gRPC caller:
`_api_get`'s HTTP-status detail, `get_now_playing`'s `tried` list of per-candidate
failures, `_require_uuid`, and the valid-`order` enumeration are all lost.

Over gRPC this is worse: `str(exc)` at `grpc_server.py:113` yields the same useless
generic string, so `CallToolResponse.error` carries no information at all.

For a tool server, actionable error text is most of the recovery signal a model has.

**Fix** — raise the SDK's `ToolError` for anticipated failures:

```python
from mcp.server.mcpserver.exceptions import ToolError  # under the existing v1/v2 shim
```

Use it in `_api_get`, `_api_post`, `_require_uuid`, `_station_list_params`,
`_facet_params`, `get_now_playing`, `get_station_by_uuid`, and `add_station`.

As defence in depth, unwrap in the bridge:

```python
error=str(exc.__cause__ or exc)
```

---

## 2. High — curated fallback returns the wrong city's station

`server.py:259` matches on *any* keyword overlap:

```python
matches = [c for keywords, c in KNOWN_STATIONS if tokens & set(keywords)]
```

Both curated entries contain `"smooth"`, so both always match together.

**Confirmed:**

```
'smooth 91.5 melbourne' -> ['smooth 95.3 Sydney (SMOOTH953)', 'smooth 91.5 Melbourne (SMOOTH915)']
'smooth fm melbourne'   -> ['smooth 95.3 Sydney (SMOOTH953)', 'smooth 91.5 Melbourne (SMOOTH915)']
```

`ranked_search` prepends curated entries **unsorted**, so
`get_now_playing("smooth 91.5 Melbourne")` connects to Sydney first and — if Sydney is
playing a track — returns Sydney's song. That is precisely the failure `AGENTS.md` says
must never happen.

**Fix** — rank by overlap and require a distinguishing token:

```python
def known_station_fallback(query: str) -> list[StationCandidate]:
    tokens = set(re.findall(r"[a-z0-9.]+", query.lower()))
    scored = [
        (len(tokens & set(kw)), c)
        for kw, c in KNOWN_STATIONS
        if len(tokens & set(kw)) >= 2
    ]
    return [c for _, c in sorted(scored, key=lambda p: -p[0])]
```

The `>= 2` threshold also subsumes the existing `"smooth" not in tokens` special case,
so that branch can go.

---

## 3. High — the same-station guard is unreachable for the case it exists to prevent

`server.py:454-464`, inside the `get_now_playing` candidate loop:

```python
if raw_title:
    return result_for(candidate, raw_title)   # <-- returns ANY station's track
if first_connected is None:
    first_connected = result_for(candidate, None)
    first_tokens = _station_tokens(candidate.name)
    continue
if len(_station_tokens(candidate.name) & first_tokens) >= 2:
    continue
break
```

The token check only runs when the candidate is *also* silent. So when the matched
station is on an ad break and the next candidate is a **different** station that *is*
playing something, the different station's track is returned — while the comment three
lines above says "never answer with a different station's song."

**Fix** — gate before the return, not after:

```python
if first_connected is not None and len(_station_tokens(candidate.name) & first_tokens) < 2:
    break
if raw_title:
    return result_for(candidate, raw_title)
```

This preserves the intended good case (same station, alternate bitrate, has a title →
return it) while making the documented invariant actually hold.

---

## 4. Medium — HLS sort is inverted in `ranked_search`

`server.py:288`:

```python
return (overlap, c.votes + c.clickcount, 0 if not c.hls else 1)
```

...and the list is sorted `reverse=True`, so HLS entries rank *above* direct streams on
ties. `search_stations` gets this right (`key=lambda c: (c.hls, ...)`, ascending).

**Confirmed** with two otherwise-identical candidates:

```
ranked_search order (hls flags): [('Jazz Radio B', True), ('Jazz Radio A', False)]
```

HLS playlists carry no ICY metadata, so the preferred candidate is guaranteed to fail and
burn a full `timeout`. Ties on `votes + clickcount` are common — there are a lot of 0/0
stations in the directory.

**Fix:** `1 if not c.hls else 0`.

---

## 5. Medium — `get_now_playing` has no overall time budget

`ranked_search` can yield ~30 deduped candidates (4 sub-queries × `limit` 8), and the loop
at `server.py:445` tries each with a full `timeout`, while `_api_get` itself can spend
`3 × timeout` across mirrors. Worst case at the default `timeout=20` is several minutes of
wall clock for a single call. `timeout` is also unclamped, so a client can pass
`timeout=6000`.

Compounding it, `grpc_server.py:78`:

```python
return asyncio.run_coroutine_threadsafe(coro, _loop).result()
```

...has no timeout and does not honour `context.time_remaining()`. A client that gives up
leaves the worker thread pinned; sixteen such calls exhaust the pool
(`max_workers=16`) and the server stops answering.

**Fix:**
- Clamp `timeout` to a sane range (e.g. 1–60) in `get_now_playing`.
- Add a `time.monotonic()` deadline spanning the candidate loop, not just each fetch.
- In the bridge, pass `context.time_remaining()` into `.result(timeout=...)` and
  `context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, ...)` on expiry.

*(Checked and ruled out: the shared event loop is **not** blocked by the sync tools — the
SDK dispatches them via `anyio.to_thread.run_sync`, so `_run`'s single-loop design is
sound.)*

---

## 6. Medium — auth token compared non-constant-time

`grpc_server.py:97`:

```python
if key == "authorization" and value == f"Bearer {self._auth_token}":
```

Use `hmac.compare_digest(value, f"Bearer {self._auth_token}")`. Small real-world risk over
a network, but it is a one-line fix on the only authentication check in the system.

**Related — worth stating explicitly in the threat model.** Read-only tools are
unauthenticated by design, and `get_now_playing` will connect to any URL the *public,
anyone-can-submit* directory returns. The scheme is checked (`http`/`https`) but the host
is not, so a station submitted with an internal address turns this into a modest
SSRF / port-probe oracle for anyone who can reach the port. The README's "put it behind a
private network or a TLS-terminating reverse proxy" note covers this in practice;
consider also rejecting private and link-local resolved addresses in
`fetch_icy_metadata`.

---

## 7. Low

- **Non-idempotent POST retried across mirrors.** `_api_post` (`server.py:118`) retries
  `json/add` on 5xx *and* on network errors. A timeout after the server accepted the write
  duplicates the station in a public directory. Skip failover for `add_station`, or retry
  only on connect-phase errors.

- **Silent truncation at 10 MB.** `resp.read(MAX_RESPONSE_BYTES)` (`server.py:98`,
  `server.py:127`) truncates rather than erroring; `json.loads` then throws
  `JSONDecodeError`, which the bare `except Exception` treats as a mirror failure — so all
  three mirrors are tried and the final error blames the network.
  `get_station_check_history(seconds=0)` is the realistic trigger, since its own docstring
  warns the full history "can be thousands of entries." Read `MAX_RESPONSE_BYTES + 1` and
  raise explicitly on overflow.

- **Unused import** `os` at `server.py:24`.

- **`filter` shadows the builtin** in `list_codecs` / `list_languages` / `list_tags`. It is
  a client-visible parameter name so renaming has a cost — `name_filter`, with the API
  mapping done internally, would be cleaner.

- **`codecExact` may not exist** in the Radio Browser API (`advanced_station_search`
  builds `field + "Exact"` uniformly). Unknown params are ignored server-side, so
  `codec_exact=True` likely silently does nothing. Worth a live probe, per the repo's own
  "probe a new endpoint live before wrapping it" rule.

- **Dockerfile drift.** The base image is `python:3.14-slim` while `.python-version` and
  `AGENTS.md` say 3.12, and `.python-version` is not copied into the image — so production
  runs a different interpreter than dev. I checked `uv.lock`: cp314 wheels are present for
  `grpcio`, so **the build does work**; this is a consistency/reproducibility issue, not a
  breakage. Either pin the base image to 3.12 or update `AGENTS.md`.

- **Unpinned uv image.** `COPY --from=ghcr.io/astral-sh/uv:latest` undercuts the
  "lockfile-pinned installs" comment sitting directly beside it. Pin a digest or version.

- **Container runs as root**, with no `USER` and no `HEALTHCHECK`.

- **No structured logging or graceful shutdown.** `print()` at startup;
  `wait_for_termination()` with no SIGTERM handler means in-flight RPCs are cut on
  `docker stop`. `grpc_server.stop(grace)` in a signal handler is a few lines.

---

## The gap that makes the rest expensive

There are no tests, no linter, and no CI — `AGENTS.md` states this as a fact rather than as
a gap.

Three of the four high/medium correctness bugs above (#2, #3, #4) live in pure,
network-free functions, and each is a five-line unit test. Adding `pytest` plus a handful
of tests would pin the invariants that `AGENTS.md` currently protects only by prose:

- `_station_tokens` — the Sydney/Melbourne separation the whole invariant rests on.
- `known_station_fallback` — curated match ranking (#2).
- `_parse_stream_title` — the `"';"`-inside-a-title edge case is exactly the kind of thing
  that regresses under a well-meaning "simplify this to a regex" refactor.
- The `get_now_playing` candidate loop with a stubbed `fetch_icy_metadata` (#3, #4) — no
  network needed, and it covers the single most important behaviour in the project.

A `ruff` pass would also have caught the unused `os` import and the `filter` shadowing for
free.

---

## Suggested sequencing

1. **One commit:** fixes #1–#4, with a unit test for each of #2, #3, #4.
2. **One commit:** #5 and #6 (timeouts, deadline propagation, `compare_digest`).
3. **Housekeeping:** the Low items, `ruff` config, and a minimal CI job running
   `ruff check` + `pytest`.
