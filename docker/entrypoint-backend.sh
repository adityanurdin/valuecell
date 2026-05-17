#!/bin/sh
set -eu

# Must match valuecell.utils.env.get_system_env_dir() on Linux (/root in container)
CONFIG_DIR="${VALUECELL_CONFIG_DIR:-/root/.config/valuecell}"
ENV_FILE="${CONFIG_DIR}/.env"
DB_PATH="${VALUECELL_DB_PATH:-/data/valuecell.db}"

mkdir -p "${CONFIG_DIR}" /data

if [ ! -f "${ENV_FILE}" ]; then
  if [ -f /app/.env.example ]; then
    cp /app/.env.example "${ENV_FILE}"
    echo "Created ${ENV_FILE} from .env.example — set API keys before trading."
  else
    touch "${ENV_FILE}"
  fi
fi

export VALUECELL_DATABASE_URL="${VALUECELL_DATABASE_URL:-sqlite:///${DB_PATH}}"

cd /app/python

echo "Initializing database at ${VALUECELL_DATABASE_URL}..."
uv run python valuecell/server/db/init_db.py

echo "Starting ValueCell API on ${API_HOST:-0.0.0.0}:${API_PORT:-8000}..."
exec uv run python -m valuecell.server.main
