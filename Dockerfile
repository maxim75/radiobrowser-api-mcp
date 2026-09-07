FROM python:3.14-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_PROJECT_ENVIRONMENT=/app/.venv

WORKDIR /app

# uv for fast, lockfile-pinned installs (runtime only, no dev tools)
COPY --from=ghcr.io/astral-sh/uv:latest /uv /bin/uv
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev

# App sources
COPY server.py app.py ./

EXPOSE 50052

HEALTHCHECK --interval=30s --timeout=10s --retries=3 --start-period=15s \
  CMD .venv/bin/python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:50052/health', timeout=5)"

CMD [".venv/bin/python", "server.py", "--transport", "streamable-http", "--host", "0.0.0.0", "--http-port", "50052"]
