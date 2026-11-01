#!/bin/sh
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
exec op run --env-file .env.references -- uv run python scripts/test-mongodb.py
