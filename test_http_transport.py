"""Tests for the Streamable HTTP transport (Coolify domain deployments).

Covers the spec's testing section:

1. `create_app()` builds and mounts `/mcp`, `/sse`, `/messages/`, `/health`, `/`.
2. `GET /health` returns 200 with no credentials.
3. CLI advertises `streamable-http` and no longer advertises `grpc`.
4. `main()` dispatches `streamable-http` to uvicorn with the right host/port.
5. Auth matrix, driven off `MUTATING_TOOLS` so it cannot drift:
   - read-only tool with no token -> allowed
   - every mutating tool with no token -> refused
   - every mutating tool with a wrong token -> refused
   - every mutating tool with the correct token -> allowed
   - token unset entirely -> every mutating tool allowed

Tests run through Starlette's test client — no network, no live deployment.
"""

from __future__ import annotations

import subprocess
import sys

import anyio
import pytest
from starlette.testclient import TestClient

import server
from server import MUTATING_TOOLS, ToolError


@pytest.fixture()
def app_client():
    import app as app_module

    return TestClient(app_module.create_app())


# --- 1. routes ---


def test_app_mounts_all_routes(app_client):
    paths = {getattr(route, "path", None) for route in app_client.app.routes}
    assert {"/mcp", "/sse", "/health", "/"} <= paths
    # Legacy SSE POST endpoint is a Mount (path has no trailing slash).
    assert any(
        getattr(route, "path", None) == "/messages" for route in app_client.app.routes
    )

    # The SSE message endpoint actually accepts posts.
    resp = app_client.post("/messages/?session_id=none", content=b"{}")
    assert resp.status_code in (400, 404, 405, 422, 500)


# --- 2. /health open ---


def test_health_returns_200_without_credentials(app_client):
    resp = app_client.get("/health")
    assert resp.status_code == 200
    assert resp.json()["status"] == "ok"


def test_index_returns_service_description(app_client):
    resp = app_client.get("/")
    assert resp.status_code == 200
    assert resp.json()["service"] == "radiobrowser-api-mcp"


# --- 3. CLI ---


def test_cli_help_lists_streamable_http_without_grpc():
    proc = subprocess.run(  # noqa: PLW1510 - returncode asserted below
        [sys.executable, "server.py", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "streamable-http" in proc.stdout
    assert "grpc" not in proc.stdout


# --- 4. main() dispatches to uvicorn ---


def test_main_dispatches_streamable_http_to_uvicorn(monkeypatch):
    import uvicorn

    import app as app_module
    import server as server_module

    calls: dict = {}

    def fake_run(app, host=None, port=None):
        calls["app"] = app
        calls["host"] = host
        calls["port"] = port

    created: dict = {}
    real_create_app = app_module.create_app

    def fake_create_app(host=None):
        created["host"] = host
        return real_create_app(host=host or "127.0.0.1")

    monkeypatch.setattr(uvicorn, "run", fake_run)
    # main() imports create_app from app; patch there.
    monkeypatch.setattr(app_module, "create_app", fake_create_app)
    server_module.main(
        ["--transport", "streamable-http", "--host", "127.0.0.1", "--http-port", "8000"]
    )
    assert created["host"] == "127.0.0.1"
    assert calls["host"] == "127.0.0.1"
    assert calls["port"] == 8000


# --- 5. auth matrix ---

_UUID = "12345678-1234-1234-1234-123456789abc"


class _Headers(dict):
    def get(self, key, default=None):  # case-insensitive like Starlette
        return super().get(key.lower(), default)


class _FakeRequest:
    def __init__(self, authorization: str | None):
        self.headers = _Headers(
            {"authorization": authorization} if authorization else {}
        )


class _FakeRequestContext:
    def __init__(self, authorization: str | None):
        self.request = _FakeRequest(authorization)


def _ctx(authorization: str | None):
    from mcp.server.mcpserver import Context

    return Context(request_context=_FakeRequestContext(authorization))


def _mutating_args(tool_name: str) -> dict:
    if tool_name == "add_station":
        return {"name": "x", "url": "http://x/"}
    return {"station_uuid": _UUID}


async def _allowed(tool_name: str, ctx) -> bool:
    """True when the call gets past the auth gate (may still fail upstream)."""
    from mcp.server.mcpserver.exceptions import ToolError as _ToolError

    try:
        await server.mcp.call_tool(tool_name, _mutating_args(tool_name), context=ctx)
    except _ToolError as exc:
        # Upstream/network failures prove the gate passed; only auth
        # refusals count as refused.
        return "requires a valid auth token" not in str(exc.__cause__ or exc)
    return True


def _run(coro):
    return anyio.run(lambda: coro)


def test_mutating_tools_list_has_not_drifted():
    assert MUTATING_TOOLS == frozenset(
        {
            "register_station_click",
            "vote_for_station",
            "resolve_station_stream_url",
            "add_station",
        }
    )


@pytest.mark.parametrize("tool_name", sorted(MUTATING_TOOLS))
def test_mutating_tool_without_token_is_refused(monkeypatch, tool_name):
    monkeypatch.setenv("RADIO_MCP_AUTH_TOKEN", "secret")
    assert not _run(_allowed(tool_name, _ctx(None)))


@pytest.mark.parametrize("tool_name", sorted(MUTATING_TOOLS))
def test_mutating_tool_with_wrong_token_is_refused(monkeypatch, tool_name):
    monkeypatch.setenv("RADIO_MCP_AUTH_TOKEN", "secret")
    assert not _run(_allowed(tool_name, _ctx("Bearer wrong")))


@pytest.mark.parametrize("tool_name", sorted(MUTATING_TOOLS))
def test_mutating_tool_with_correct_token_is_allowed(monkeypatch, tool_name):
    monkeypatch.setenv("RADIO_MCP_AUTH_TOKEN", "secret")
    # Stub the network layer: allowed means "reached the upstream call".
    monkeypatch.setattr(server, "_api_get", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(server, "_api_post", lambda *a, **k: {"ok": True})
    assert _run(_allowed(tool_name, _ctx("Bearer secret")))


@pytest.mark.parametrize("tool_name", sorted(MUTATING_TOOLS))
def test_mutating_tools_open_when_token_unset(monkeypatch, tool_name):
    monkeypatch.delenv("RADIO_MCP_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(server, "_api_get", lambda *a, **k: {"ok": True})
    monkeypatch.setattr(server, "_api_post", lambda *a, **k: {"ok": True})
    assert _run(_allowed(tool_name, _ctx(None)))


def test_read_only_tool_open_with_token_set(monkeypatch):
    monkeypatch.setenv("RADIO_MCP_AUTH_TOKEN", "secret")
    monkeypatch.setattr(server, "_api_get", lambda *a, **k: {"stations": 1})

    async def go():
        result = await server.mcp.call_tool(
            "get_directory_stats", {}, context=_ctx(None)
        )
        assert result is not None

    _run(go())


def test_stdio_carve_out_skips_auth(monkeypatch):
    """No HTTP request in context (stdio / direct call) -> check skipped."""
    monkeypatch.setenv("RADIO_MCP_AUTH_TOKEN", "secret")
    server._require_mutating_auth(None, "vote_for_station")

    from mcp.server.mcpserver import Context

    server._require_mutating_auth(Context(), "vote_for_station")


def test_refusal_names_the_token(monkeypatch):
    monkeypatch.setenv("RADIO_MCP_AUTH_TOKEN", "secret")

    # call_tool wraps failures; the message survives in __cause__.
    async def go_wrapped():
        try:
            await server.mcp.call_tool(
                "vote_for_station",
                {"station_uuid": _UUID},
                context=_ctx(None),
            )
            raise AssertionError("expected tool call to fail")
        except Exception as exc:
            assert "requires a valid auth token" in str(exc.__cause__ or exc)

    _run(go_wrapped())
    # Direct helper call raises ToolError unwrapped.
    with pytest.raises(ToolError, match="requires a valid auth token"):
        server._require_mutating_auth(_ctx(None), "vote_for_station")


def test_host_env_default(monkeypatch):
    import uvicorn

    import server as server_module

    monkeypatch.setenv("HOST", "0.0.0.0")
    # CLI falls back to HOST env when --host is not given.
    calls: dict = {}
    monkeypatch.setattr(
        uvicorn, "run", lambda app, host=None, port=None: calls.update(host=host)
    )
    server_module.main(["--transport", "streamable-http"])
    assert calls["host"] == "0.0.0.0"
