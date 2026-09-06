"""Container HEALTHCHECK probe: standard gRPC health check, no MCP calls.

Exits 0 when the server reports SERVING, 1 otherwise. Runs as the
non-root app user inside the image; stdlib + grpcio only.
"""

from __future__ import annotations

import os
import sys

import grpc
from grpc_health.v1 import health_pb2, health_pb2_grpc

STATUS_NAMES = {0: "UNKNOWN", 1: "SERVING", 2: "NOT_SERVING", 3: "SERVICE_UNKNOWN"}


def main() -> int:
    port = int(os.environ.get("HEALTHCHECK_PORT", "50051"))
    channel = grpc.insecure_channel(f"127.0.0.1:{port}")
    try:
        stub = health_pb2_grpc.HealthStub(channel)
        resp = stub.Check(
            health_pb2.HealthCheckRequest(service="radiomcp.RadioMcpService"),
            timeout=5,
        )
        print(f"health: {STATUS_NAMES.get(resp.status, resp.status)}")
        return 0 if resp.status == 1 else 1  # 1 == SERVING
    except grpc.RpcError as exc:
        print(f"health check failed: {exc.code()}: {exc.details()}")
        return 1
    finally:
        channel.close()


if __name__ == "__main__":
    sys.exit(main())
