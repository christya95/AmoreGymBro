#!/bin/sh
set -eu
# Production owns scheduled sync; this launcher is for Mac development.
export SYNC_ENABLED=false GARMIN_SYNC_ENABLED=false WEBHOOK_WORKER_ENABLED=false
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
if ! command -v op >/dev/null 2>&1; then
  printf '%s\n' 'Install 1Password CLI and enable its desktop integration first.' >&2
  exit 1
fi
if [ ! -f .env.app.references ]; then
  printf '%s\n' 'Set up this project’s 1Password references before starting.' >&2
  exit 1
fi
uv run python scripts/ensure-nas-tunnel.py
# 1Password may ask for local unlock/Touch ID. Never source or print secret values.
exec op run --env-file .env.app.references -- uv run uvicorn app.main:app --host 127.0.0.1 --port 8787 "$@"
