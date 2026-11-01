#!/bin/sh
# Install beside the NAS Compose files as start-coach.sh; not a Mac launcher.
set -eu
cd "$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"
test -f nas-coach.compose.yml
test -f nas-tailscale.compose.yml
test -f tailscale/serve.json
OP_SERVICE_ACCOUNT_TOKEN=$(cat .secrets/onepassword-token)
export OP_SERVICE_ACCOUNT_TOKEN
exec ./bin/op run --env-file .env.app.references -- sh -eu -c '
  unset OP_SERVICE_ACCOUNT_TOKEN
  docker compose --env-file .env.coach -f nas-coach.compose.yml -f nas-tailscale.compose.yml config --quiet
  # A replacement coach gets a new network namespace; restart alone is insufficient.
  docker compose --env-file .env.coach -f nas-coach.compose.yml -f nas-tailscale.compose.yml stop tailscale
  exec docker compose --env-file .env.coach -f nas-coach.compose.yml -f nas-tailscale.compose.yml up -d --force-recreate --wait --wait-timeout 120 coach tailscale
'
