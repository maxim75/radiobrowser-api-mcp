FROM python:3.12-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# uv for fast, lockfile-pinned installs (runtime only, no dev tools)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App sources (generated stubs included so the image builds without grpcio-tools)
COPY server.py grpc_server.py healthcheck.py radio_mcp.proto radio_mcp_pb2.py radio_mcp_pb2_grpc.py ./

# Run as non-root: own the venv + sources after the root-owned install steps.
RUN useradd --create-home --uid 10001 appuser \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 50051

HEALTHCHECK --interval=30s --timeout=10s --start-period=20s --retries=3 \
    CMD [".venv/bin/python", "healthcheck.py"]

CMD [".venv/bin/python", "server.py", "--transport", "grpc", "--host", "0.0.0.0", "--port", "50051"]
