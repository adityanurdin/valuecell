# ValueCell backend — multi-arch (amd64, arm64, arm/v7, arm/v5 for older Pis)
# Uses official Python image + pip-installed uv (ghcr.io/astral-sh/uv has no arm/v6 manifest).
FROM python:3.12-slim-bookworm

WORKDIR /app/python

RUN apt-get update \
    && apt-get install -y --no-install-recommends curl ca-certificates \
    && rm -rf /var/lib/apt/lists/* \
    && pip install --no-cache-dir uv

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

HEALTHCHECK --interval=30s --timeout=5s --start-period=120s --retries=3 \
    CMD curl -fsS "http://127.0.0.1:${API_PORT}/api/v1/healthz" || exit 1

ENTRYPOINT ["/entrypoint.sh"]
