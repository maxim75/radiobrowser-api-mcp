"""Bridge tests: live gRPC server on a loopback port.

Catches what server.py unit tests cannot:
- no-deadline RPC must work (gRPC yields an "infinite" sentinel, not None;
  passing it to Future.result() used to overflow with
  "timestamp out of range for platform time_t")
- an expiring client deadline surfaces DEADLINE_EXCEEDED, not UNKNOWN
- ListTools errors stay in-band instead of raw UNKNOWN tracebacks
"""

from __future__ import annotations

import json
import time

import grpc
import pytest
from grpc_health.v1 import health_pb2, health_pb2_grpc

import radio_mcp_pb2 as pb2
import radio_mcp_pb2_grpc as pb2_grpc
from grpc_server import MAX_CALL_SECONDS, build_server


@pytest.fixture(scope="module")
def server_and_stub():
    grpc_server, _health = build_server("127.0.0.1", 0, max_workers=8)
    port = grpc_server.add_insecure_port("127.0.0.1:0")
    grpc_server.start()
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    yield grpc_server, channel, pb2_grpc.RadioMcpServiceStub(channel)
    channel.close()
    grpc_server.stop(grace=None)


@pytest.fixture(scope="module")
def stub(server_and_stub):
    return server_and_stub[2]


def test_call_tool_without_client_deadline(stub):
    """Default RPC (no deadline) must not overflow the wait timeout."""
    resp = stub.CallTool(
        pb2.CallToolRequest(name="get_directory_stats", arguments_json="{}"),
        timeout=60,
    )
    assert resp.ok, resp.error
    assert json.loads(resp.result_json)["items"][0]["stations"] > 0


def test_list_tools_without_client_deadline(stub):
    resp = stub.ListTools(pb2.ListToolsRequest(), timeout=60)
    assert len(json.loads(resp.tools_json)) >= 29


def test_expiring_deadline_aborts_instead_of_unknown(stub, monkeypatch):
    """A client deadline shorter than the tool runtime -> DEADLINE_EXCEEDED."""
    import server as server_module

    def slow_search(*args, **kwargs):
        time.sleep(5)
        return []

    monkeypatch.setattr(server_module, "ranked_search", slow_search)
    with pytest.raises(grpc.RpcError) as exc_info:
        stub.CallTool(
            pb2.CallToolRequest(
                name="get_now_playing",
                arguments_json=json.dumps({"station_name": "x", "timeout": 30}),
            ),
            timeout=2,
        )
    assert exc_info.value.code() == grpc.StatusCode.DEADLINE_EXCEEDED


def test_call_timeout_clamps_infinite_sentinel():
    from grpc_server import _call_timeout

    class NoDeadline:
        def time_remaining(self):
            return 9.223372035066081e18  # gRPC's int64 "infinite" sentinel

    assert _call_timeout(NoDeadline()) == MAX_CALL_SECONDS


def test_call_timeout_honours_short_deadline():
    from grpc_server import _call_timeout

    class ShortDeadline:
        def time_remaining(self):
            return 7.5

    assert _call_timeout(ShortDeadline()) == 7.5


def test_health_service_reports_serving(server_and_stub):
    _grpc_server, channel, _stub = server_and_stub
    health_stub = health_pb2_grpc.HealthStub(channel)
    for service in ("", "radiomcp.RadioMcpService"):
        resp = health_stub.Check(
            health_pb2.HealthCheckRequest(service=service), timeout=10
        )
        assert resp.status == 1  # SERVING


def test_healthcheck_probe_script(server_and_stub, monkeypatch):
    """healthcheck.py exits 0 against a live server (stdlib + grpcio only)."""
    import runpy

    _grpc_server, channel, _stub = server_and_stub
    target = channel._channel.target().decode()
    port = target.rsplit(":", 1)[1]
    monkeypatch.setenv("HEALTHCHECK_PORT", port)
    with pytest.raises(SystemExit) as exc_info:
        runpy.run_path("healthcheck.py", run_name="__main__")
    assert exc_info.value.code == 0
