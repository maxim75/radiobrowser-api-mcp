"""gRPC bridge exposing every MCP tool in server.py for remote connections.

Contract: radio_mcp.proto (RadioMcpService with CallTool + ListTools).
Because tool arguments/results travel as JSON, the bridge needs no changes
when tools are added — ListTools advertises names + JSON schemas.

Run:
    uv run server.py --transport grpc --host 127.0.0.1 --port 50051
"""

from __future__ import annotations

import asyncio
import json
from concurrent import futures

import grpc

import radio_mcp_pb2 as pb2
import radio_mcp_pb2_grpc as pb2_grpc
import server


def _serialize_content(result) -> str:
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
    if len(items) == 1:
        return json.dumps(items[0])
    return json.dumps(items)


class RadioMcpServicer(pb2_grpc.RadioMcpServiceServicer):
    def CallTool(self, request, context):
        try:
            arguments = json.loads(request.arguments_json or "{}")
        except json.JSONDecodeError as exc:
            return pb2.CallToolResponse(ok=False, error=f"invalid arguments_json: {exc}")
        try:
            result = asyncio.run(server.mcp.call_tool(request.name, arguments))
            return pb2.CallToolResponse(ok=True, result_json=_serialize_content(result))
        except Exception as exc:  # tool raised (unknown name, validation, upstream)
            # Return ok=False in-band; do NOT set a gRPC error status here or
            # the payload is discarded and the client sees only INTERNAL.
            return pb2.CallToolResponse(ok=False, error=str(exc))

    def ListTools(self, request, context):
        tools = asyncio.run(server.mcp.list_tools())
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


def serve(host: str = "127.0.0.1", port: int = 50051, max_workers: int = 10) -> None:
    grpc_server = grpc.server(futures.ThreadPoolExecutor(max_workers=max_workers))
    pb2_grpc.add_RadioMcpServiceServicer_to_server(RadioMcpServicer(), grpc_server)
    grpc_server.add_insecure_port(f"{host}:{port}")
    grpc_server.start()
    print(f"radiobrowser-api-mcp gRPC listening on {host}:{port}")
    grpc_server.wait_for_termination()


if __name__ == "__main__":
    serve()
