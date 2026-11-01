#!/bin/sh
set -eu
if [ "$(id -u)" = "0" ]; then
  chown app:app /data
  exec su -s /bin/sh app -c "cd /app && exec uvicorn app.main:app --host 0.0.0.0 --port 8787"
fi
exec uvicorn app.main:app --host 0.0.0.0 --port 8787
