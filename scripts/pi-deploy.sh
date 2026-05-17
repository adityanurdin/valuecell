#!/usr/bin/env bash
# Deploy ValueCell backend + frontend on Raspberry Pi (same host).
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"

COMPOSE=(docker compose -f docker-compose.yml -f docker-compose.rpi.yml)
FRONTEND_DOCKERFILE="${FRONTEND_DOCKERFILE:-docker/frontend.nginx.Dockerfile}"
export FRONTEND_DOCKERFILE

info() { echo "[pi-deploy] $*"; }
err() { echo "[pi-deploy] ERROR: $*" >&2; }

ensure_swap_hint() {
  local mem_kb
  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  if [[ "${mem_kb}" -gt 0 && "${mem_kb}" -lt 900000 ]]; then
    info "Low RAM detected (~$((mem_kb / 1024))MB). Enable 1–2GB swap if the build fails:"
    info "  sudo dphys-swapfile swapoff && sudo sed -i 's/CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile && sudo dphys-swapfile setup && sudo dphys-swapfile swapon"
  fi
}

build_frontend_on_host() {
  if [[ "${SKIP_FRONTEND_BUILD:-0}" == "1" ]]; then
    if [[ ! -f frontend/build/client/index.html ]]; then
      err "SKIP_FRONTEND_BUILD=1 but frontend/build/client/index.html is missing"
      exit 1
    fi
    info "Using existing frontend/build/client (SKIP_FRONTEND_BUILD=1)"
    return 0
  fi

  if [[ -f frontend/build/client/index.html ]]; then
    info "Reusing frontend/build/client (delete it to force rebuild)"
    return 0
  fi

  if [[ "${FRONTEND_DOCKERFILE}" == "docker/frontend.Dockerfile" ]]; then
    info "FRONTEND_DOCKERFILE=docker/frontend.Dockerfile — skipping host build (Docker will build UI)"
    return 0
  fi

  ensure_swap_hint
  info "Building frontend on host (for nginx image)..."

  export PATH="${HOME}/.bun/bin:${PATH}"
  if ! command -v bun >/dev/null 2>&1; then
    info "Installing Bun..."
    curl -fsSL https://bun.sh/install | bash
    export PATH="${HOME}/.bun/bin:${PATH}"
  fi

  if ! command -v bun >/dev/null 2>&1; then
    err "Bun is not available on this CPU/OS."
    err "On Pi B+ (ARMv6), build on a PC then copy:"
    err "  rsync -av frontend/build/client/ pi@\$(hostname):${ROOT}/frontend/build/client/"
    err "  SKIP_FRONTEND_BUILD=1 $0"
    exit 1
  fi

  export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=384}"
  (
    cd frontend
    bun install --frozen-lockfile
    VITE_API_BASE_URL=/api/v1 bun run build
  )

  if [[ ! -f frontend/build/client/index.html ]]; then
    err "Frontend build failed — index.html not found"
    exit 1
  fi
  info "Frontend build OK: frontend/build/client"
}

main() {
  if [[ ! -f .env ]]; then
    if [[ -f .env.example ]]; then
      cp .env.example .env
      info "Created .env from .env.example — add API keys before trading"
    fi
  fi

  build_frontend_on_host

  info "Docker compose: FRONTEND_DOCKERFILE=${FRONTEND_DOCKERFILE}"
  "${COMPOSE[@]}" up -d --build "$@"

  info "Done."
  info "  UI:  http://$(hostname -I 2>/dev/null | awk '{print $1}'):${FRONTEND_PORT:-1420}"
  info "  API: http://$(hostname -I 2>/dev/null | awk '{print $1}'):${API_PORT:-8000}/api/v1/healthz"
}

main "$@"
