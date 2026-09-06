FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# uv for fast, lockfile-pinned installs (runtime only, no dev tools)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App sources (generated stubs included so the image builds without grpcio-tools)
COPY server.py grpc_server.py radio_mcp.proto radio_mcp_pb2.py radio_mcp_pb2_grpc.py ./

EXPOSE 50052

CMD [".venv/bin/python", "server.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--http-port", "50052"]
