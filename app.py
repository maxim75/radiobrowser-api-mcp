"""ASGI application: MCP over Streamable HTTP (+ legacy SSE) for remote clients.

Owned by this module so `server.py` stays focused on tool definitions.
`server.main()` runs uvicorn against `create_app()` for the
`--transport streamable-http` branch.

Routes:

| Path       | Purpose                                      | Auth     |
|------------|----------------------------------------------|----------|
| `/mcp`     | Streamable HTTP transport (current standard) | per-tool |
| `/sse`     | Legacy SSE transport, `/messages/` for posts | per-tool |
| `/health`  | Liveness probe for Coolify and Docker        | open     |
| `/`        | Short service description                    | open     |

`stateless_http=True`: no MCP session is pinned to a worker, so Coolify can
restart or scale the container without breaking connected `/mcp` clients.
"""

from __future__ import annotations

import os

from starlette.applications import Starlette
from starlette.responses import JSONResponse

import server

HOST = os.environ.get("HOST", "0.0.0.0")
PORT = int(os.environ.get("PORT", "50052"))


@server.mcp.custom_route("/health", methods=["GET"])
async def health(request):  # type: ignore[no-untyped-def]  # Starlette Request -> JSONResponse
    """Liveness probe. Never touches the upstream Radio Browser API, so an
    upstream outage does not make Coolify restart a healthy container."""
    return JSONResponse({"status": "ok", "service": "radiobrowser-api-mcp"})


@server.mcp.custom_route("/", methods=["GET"])
async def index(request):  # type: ignore[no-untyped-def]
    return JSONResponse(
        {
            "service": "radiobrowser-api-mcp",
            "description": "MCP server for the Radio Browser directory plus live ICY/Shoutcast now-playing metadata.",
            "transports": {"streamable_http": "/mcp", "sse": "/sse"},
            "health": "/health",
        }
    )


def create_app(host: str = HOST) -> Starlette:
    """Build the ASGI app: Streamable HTTP base + legacy SSE routes."""
    app = server.mcp.streamable_http_app(stateless_http=True, host=host)
    existing = {getattr(route, "path", None) for route in app.routes}
    for route in server.mcp.sse_app(host=host).routes:
        if getattr(route, "path", None) not in existing:
            app.routes.append(route)
    return app


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(create_app(), host=HOST, port=PORT)
