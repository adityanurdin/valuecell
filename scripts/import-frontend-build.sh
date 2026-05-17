#!/usr/bin/env bash
# Unpack frontend-build.tar.gz from export-frontend-build.sh
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
ARCHIVE="${1:-${ROOT}/frontend-build.tar.gz}"

if [[ ! -f "${ARCHIVE}" ]]; then
  echo "Usage: $0 [frontend-build.tar.gz]" >&2
  exit 1
fi

cd "$ROOT"
mkdir -p frontend/build
rm -rf frontend/build/client
tar -xzf "${ARCHIVE}" -C frontend/build

if [[ ! -f frontend/build/client/index.html ]]; then
  echo "ERROR: index.html missing after extract" >&2
  exit 1
fi

echo "[import-frontend] OK: frontend/build/client ready"
