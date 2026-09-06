"""Tests for the Streamable HTTP transport (Coolify domain deployments).

- `--transport streamable-http` is advertised by the CLI.
- `server.main()` dispatches streamable-http to `mcp.run()` with the
  right host/port/path (gRPC stays available as a separate mode).
"""

from __future__ import annotations

import subprocess
import sys


def test_cli_help_lists_streamable_http():
    proc = subprocess.run(  # noqa: PLW1510 - returncode asserted below
        [sys.executable, "server.py", "--help"],
        capture_output=True,
        text=True,
        timeout=60,
    )
    assert proc.returncode == 0, proc.stderr
    assert "streamable-http" in proc.stdout


def test_main_dispatches_streamable_http(monkeypatch):
    import server as server_module

    calls: dict = {}

    def fake_run(transport="stdio", **kwargs):
        calls["transport"] = transport
        calls.update(kwargs)

    monkeypatch.setattr(server_module.mcp, "run", fake_run)
    server_module.main(
        ["--transport", "streamable-http", "--http-port", "8000", "--path", "/mcp"]
    )
    assert calls["transport"] == "streamable-http"
    assert calls["port"] == 50052
    assert calls["streamable_http_path"] == "/mcp"


def test_main_keeps_grpc_mode(monkeypatch):
    import server as server_module

    calls: dict = {}

    def fake_serve(host, port):
        calls["host"] = host
        calls["port"] = port

    monkeypatch.setattr("grpc_server.serve", fake_serve)
    server_module.main(["--transport", "grpc", "--host", "127.0.0.1", "--port", "50051"])
    assert calls == {"host": "127.0.0.1", "port": 50051}
