#!/usr/bin/env bash
# Build frontend on PC/Mac (Bun) and pack for Raspberry Pi import.
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$ROOT"
OUT="${ROOT}/frontend-build.tar.gz"

info() { echo "[export-frontend] $*"; }
err() { echo "[export-frontend] ERROR: $*" >&2; }

if ! command -v bun >/dev/null 2>&1; then
  err "Bun required on this machine. Install: https://bun.sh"
  exit 1
fi

info "Installing dependencies..."
(
  cd frontend
  bun install --frozen-lockfile
  VITE_API_BASE_URL=/api/v1 bun run build
)

if [[ ! -f frontend/build/client/index.html ]]; then
  err "Build failed — frontend/build/client/index.html missing"
  exit 1
fi

info "Packing frontend/build/client -> ${OUT}"
tar -czf "${OUT}" -C frontend/build client

info "Done. Copy to Pi:"
info "  scp ${OUT} homepi@<pi-ip>:~/valuecell/"
info "  ssh homepi@<pi-ip> 'cd ~/valuecell && ./scripts/pi-deploy.sh'"
