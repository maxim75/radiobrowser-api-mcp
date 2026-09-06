"""gRPC bridge exposing every MCP tool in server.py for remote connections.

Contract: radio_mcp.proto (RadioMcpService with CallTool + ListTools).
Because tool arguments/results travel as JSON, the bridge needs no changes
when tools are added — ListTools advertises names + JSON schemas.

Run:
    uv run server.py --transport grpc --host 127.0.0.1 --port 50051
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import hmac
import json
import os
import threading

import grpc
from grpc_health.v1 import health, health_pb2, health_pb2_grpc

import radio_mcp_pb2 as pb2
import radio_mcp_pb2_grpc as pb2_grpc
import server


def _serialize_content(result) -> str:
    """Serialize MCP content blocks as an explicit envelope.

    Envelope shape disambiguates "object result" from "one-item list":
        {"items": [...], "count": N}
    Clients must read result_json["items"], never the bare payload.
    """
    blocks = getattr(result, "content", [result])
    items = []
    for block in blocks or []:
        text = getattr(block, "text", None)
        if text is not None:
            try:
                items.append(json.loads(text))
                continue
            except json.JSONDecodeError:
                pass
        if hasattr(block, "model_dump"):
            items.append(block.model_dump(mode="json"))
        else:
            items.append(str(block))
    return json.dumps({"items": items, "count": len(items)})


# Tools that mutate the public directory (or submit to it). When the server
# is started with RADIO_MCP_AUTH_TOKEN set, these require
# `authorization: Bearer <token>` metadata; read-only tools stay open.
MUTATING_TOOLS = frozenset(
    {
        "register_station_click",
        "vote_for_station",
        "resolve_station_stream_url",
        "add_station",
    }
)


_loop: asyncio.AbstractEventLoop | None = None
_loop_lock = threading.Lock()


def _run(coro, timeout: float | None = None):
    """Run a coroutine on a shared loop instead of asyncio.run() per call.

    asyncio.run() builds and tears down a loop (and its threadpool) on every
    RPC; a single long-lived loop in its own thread avoids that churn.
    `timeout` bounds the wait so an abandoned client can't pin a worker.
    """
    global _loop
    with _loop_lock:
        if _loop is None:
            _loop = asyncio.new_event_loop()
            threading.Thread(target=_loop.run_forever, daemon=True).start()
    return asyncio.run_coroutine_threadsafe(coro, _loop).result(timeout=timeout)


MAX_CALL_SECONDS = 300.0


def _call_timeout(context) -> float:
    """Seconds to wait for a call: the client's deadline, clamped.

    A client with no deadline yields an int64 "infinite" sentinel, not None;
    passing it to Future.result() overflows the platform time_t.
    """
    try:
        remaining = context.time_remaining()
    except Exception:
        return MAX_CALL_SECONDS
    if remaining is None or remaining > MAX_CALL_SECONDS:
        return MAX_CALL_SECONDS
    return max(remaining, 1.0)


class RadioMcpServicer(pb2_grpc.RadioMcpServiceServicer):
    """Servicer with optional token auth for the mutating tools.

    Args:
        auth_token: when set, CallTool requests for MUTATING_TOOLS must carry
            matching `authorization: Bearer <token>` gRPC metadata. Read-only
            tools stay open. When None/empty, everything is open (local use).
    """

    def __init__(self, auth_token: str = ""):
        self._auth_token = auth_token

    def _authorized(self, context, tool_name: str) -> bool:
        if not self._auth_token or tool_name not in MUTATING_TOOLS:
            return True
        for key, value in context.invocation_metadata():
            if key == "authorization" and hmac.compare_digest(
                value, f"Bearer {self._auth_token}"
            ):
                return True
        return False

    def CallTool(self, request, context):
        if not self._authorized(context, request.name):
            return pb2.CallToolResponse(
                ok=False, error=f"tool {request.name!r} requires a valid auth token"
            )
        try:
            arguments = json.loads(request.arguments_json or "{}")
        except json.JSONDecodeError as exc:
            return pb2.CallToolResponse(
                ok=False, error=f"invalid arguments_json: {exc}"
            )
        try:
            # Defence in depth: the MCP SDK wraps tool failures and keeps only
            # a generic message ("Error executing tool X"); the actionable
            # detail lives in __cause__. Unwrap it for the caller.
            result = _run(
                server.mcp.call_tool(request.name, arguments),
                timeout=_call_timeout(context),
            )
            return pb2.CallToolResponse(ok=True, result_json=_serialize_content(result))
        except (concurrent.futures.TimeoutError, grpc.RpcError) as exc:
            # Client went away or its deadline expired: surface, don't mask.
            context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, f"call timed out: {exc}")
        except Exception as exc:  # tool raised (unknown name, validation, upstream)
            # Return ok=False in-band; do NOT set a gRPC error status here or
            # the payload is discarded and the client sees only INTERNAL.
            return pb2.CallToolResponse(ok=False, error=str(exc.__cause__ or exc))

    def ListTools(self, request, context):
        try:
            tools = _run(server.mcp.list_tools(), timeout=_call_timeout(context))
        except (concurrent.futures.TimeoutError, grpc.RpcError) as exc:
            context.abort(grpc.StatusCode.DEADLINE_EXCEEDED, f"call timed out: {exc}")
        except Exception as exc:
            context.abort(
                grpc.StatusCode.INTERNAL, f"list tools failed: {exc.__cause__ or exc}"
            )
        payload = [
            {
                "name": t.name,
                "description": t.description,
                "input_schema": t.input_schema
                if isinstance(t.input_schema, dict)
                else t.input_schema.model_dump(mode="json"),
            }
            for t in tools
        ]
        return pb2.ListToolsResponse(tools_json=json.dumps(payload))


def build_server(
    host: str = "127.0.0.1",
    port: int = 50051,
    max_workers: int = 32,
    auth_token: str = "",
) -> tuple[grpc.Server, health.HealthServicer]:
    """Create (but do not start) the gRPC server with health checking.

    Split out from serve() so tests can bind a loopback port without
    duplicating servicer wiring; the standard grpc.health.v1 service reports
    SERVING for "" and "radiomcp.RadioMcpService" once started.
    """
    grpc_server = grpc.server(
        concurrent.futures.ThreadPoolExecutor(max_workers=max_workers)
    )
    pb2_grpc.add_RadioMcpServiceServicer_to_server(
        RadioMcpServicer(auth_token=auth_token),
        grpc_server,
    )
    health_servicer = health.HealthServicer()
    health_pb2_grpc.add_HealthServicer_to_server(health_servicer, grpc_server)
    for service in ("", "radiomcp.RadioMcpService"):
        health_servicer.set(service, health_pb2.HealthCheckResponse.SERVING)
    grpc_server.add_insecure_port(f"{host}:{port}")
    return grpc_server, health_servicer


def serve(host: str = "127.0.0.1", port: int = 50051, max_workers: int = 32) -> None:
    grpc_server, _health = build_server(
        host,
        port,
        max_workers,
        auth_token=os.environ.get("RADIO_MCP_AUTH_TOKEN", ""),
    )
    grpc_server.start()
    print(f"radiobrowser-api-mcp gRPC listening on {host}:{port}")
    grpc_server.wait_for_termination()


if __name__ == "__main__":
    serve()
