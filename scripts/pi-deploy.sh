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

machine_arch() {
  uname -m 2>/dev/null || echo unknown
}

is_armv6() {
  case "$(machine_arch)" in
    armv6l | armv5tel) return 0 ;;
  esac
  return 1
}

bun_works() {
  command -v bun >/dev/null 2>&1 || return 1
  bun --version >/dev/null 2>&1
}

node_major() {
  if ! command -v node >/dev/null 2>&1; then
    echo 0
    return
  fi
  node -p "parseInt(process.versions.node.split('.')[0], 10)" 2>/dev/null || echo 0
}

ensure_swap_hint() {
  local mem_kb
  mem_kb="$(awk '/MemTotal/ {print $2}' /proc/meminfo 2>/dev/null || echo 0)"
  if [[ "${mem_kb}" -gt 0 && "${mem_kb}" -lt 900000 ]]; then
    info "Low RAM (~$((mem_kb / 1024))MB). Use 1–2GB swap if build fails:"
    info "  sudo dphys-swapfile swapoff && sudo sed -i 's/CONF_SWAPSIZE=.*/CONF_SWAPSIZE=2048/' /etc/dphys-swapfile && sudo dphys-swapfile setup && sudo dphys-swapfile swapon"
  fi
}

try_import_tarball() {
  local archive="${ROOT}/frontend-build.tar.gz"
  if [[ -f "${archive}" ]]; then
    info "Found ${archive} — importing pre-built UI..."
    "${ROOT}/scripts/import-frontend-build.sh" "${archive}"
    return 0
  fi
  return 1
}

build_with_npm() {
  local major
  major="$(node_major)"
  if [[ "${major}" -lt 20 ]]; then
    return 1
  fi
  info "Building with Node ${major} + npm..."
  export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=384}"
  (
    cd frontend
    if [[ -f package-lock.json ]]; then
      npm ci
    else
      npm install
    fi
    VITE_API_BASE_URL=/api/v1 npm run build
  )
}

build_with_bun() {
  export PATH="${HOME}/.bun/bin:${PATH}"
  if ! bun_works; then
    if [[ -x "${HOME}/.bun/bin/bun" ]]; then
      info "Removing broken Bun binary (wrong CPU arch)..."
      rm -f "${HOME}/.bun/bin/bun"
    fi
    if ! command -v bun >/dev/null 2>&1; then
      info "Installing Bun..."
      curl -fsSL https://bun.sh/install | bash || true
      export PATH="${HOME}/.bun/bin:${PATH}"
    fi
  fi
  if ! bun_works; then
    return 1
  fi
  info "Building with Bun..."
  export NODE_OPTIONS="${NODE_OPTIONS:---max-old-space-size=384}"
  (
    cd frontend
    bun install --frozen-lockfile
    VITE_API_BASE_URL=/api/v1 bun run build
  )
}

print_armv6_help() {
  err "This Pi ($(machine_arch)) cannot run Bun or modern Node (Pi B+ / ARMv6)."
  err ""
  err "Build the UI on a PC/Mac, then deploy on the Pi:"
  err "  # On PC (in valuecell repo):"
  err "  ./scripts/export-frontend-build.sh"
  err "  scp frontend-build.tar.gz homepi@<pi-ip>:~/valuecell/"
  err ""
  err "  # On Pi:"
  err "  ./scripts/pi-deploy.sh"
  err ""
  err "Or copy only the build folder:"
  err "  rsync -av frontend/build/client/ homepi@<pi-ip>:~/valuecell/frontend/build/client/"
  err "  SKIP_FRONTEND_BUILD=1 ./scripts/pi-deploy.sh"
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
    info "Reusing frontend/build/client (rm -rf frontend/build/client to rebuild)"
    return 0
  fi

  if [[ "${FRONTEND_DOCKERFILE}" == "docker/frontend.Dockerfile" ]]; then
    if is_armv6; then
      err "docker/frontend.Dockerfile cannot build on ARMv6 inside Docker."
      print_armv6_help
      exit 1
    fi
    info "FRONTEND_DOCKERFILE=docker/frontend.Dockerfile — Docker will build UI"
    return 0
  fi

  if try_import_tarball; then
    return 0
  fi

  ensure_swap_hint
  info "Building frontend on host (arch=$(machine_arch))..."

  if is_armv6; then
    print_armv6_help
    exit 1
  fi

  if build_with_bun || build_with_npm; then
    if [[ -f frontend/build/client/index.html ]]; then
      info "Frontend build OK: frontend/build/client"
      return 0
    fi
  fi

  err "Frontend build failed on this device."
  if ! bun_works && [[ "$(node_major)" -lt 20 ]]; then
    print_armv6_help
  fi
  exit 1
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
  local ip
  ip="$(hostname -I 2>/dev/null | awk '{print $1}')"
  info "  UI:  http://${ip:-localhost}:${FRONTEND_PORT:-1420}"
  info "  API: http://${ip:-localhost}:${API_PORT:-8000}/api/v1/healthz"
}

main "$@"
