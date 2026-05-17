# ValueCell backend API + in-process strategy agents
FROM ghcr.io/astral-sh/uv:python3.12-bookworm-slim

WORKDIR /app/python

# Runtime deps (healthcheck, optional browser automation)
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Install Python dependencies first (better layer cache)
COPY python/pyproject.toml python/uv.lock ./
RUN uv sync --frozen --no-install-project

COPY python/ ./
COPY .env.example /app/.env.example

RUN uv sync --frozen

COPY docker/entrypoint-backend.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

ENV PYTHONUNBUFFERED=1 \
    PYTHONIOENCODING=utf-8 \
    API_HOST=0.0.0.0 \
    API_PORT=8000 \
    VALUECELL_CONFIG_DIR=/root/.config/valuecell \
    VALUECELL_DB_PATH=/data/valuecell.db

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=40s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${API_PORT}/api/v1/healthz" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
