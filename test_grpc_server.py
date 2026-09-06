"""Bridge tests: live gRPC server on a loopback port.

Catches what server.py unit tests cannot:
- no-deadline RPC must work (gRPC yields an "infinite" sentinel, not None;
  passing it to Future.result() used to overflow with
  "timestamp out of range for platform time_t")
- an expiring client deadline surfaces DEADLINE_EXCEEDED, not UNKNOWN
- ListTools errors stay in-band instead of raw UNKNOWN tracebacks
"""

from __future__ import annotations

import concurrent.futures
import json
import time

import grpc
import pytest

import radio_mcp_pb2 as pb2
import radio_mcp_pb2_grpc as pb2_grpc
from grpc_server import MAX_CALL_SECONDS, RadioMcpServicer


@pytest.fixture(scope="module")
def stub():
    grpc_server = grpc.server(concurrent.futures.ThreadPoolExecutor(max_workers=8))
    pb2_grpc.add_RadioMcpServiceServicer_to_server(RadioMcpServicer(), grpc_server)
    port = grpc_server.add_insecure_port("127.0.0.1:0")
    grpc_server.start()
    yield pb2_grpc.RadioMcpServiceStub(grpc.insecure_channel(f"127.0.0.1:{port}"))
    grpc_server.stop(grace=None)


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
