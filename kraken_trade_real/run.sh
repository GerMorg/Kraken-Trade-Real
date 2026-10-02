#!/usr/bin/with-contenv bashio
set -euo pipefail

mkdir -p /data/logs /data/state

exec python3 -m app.main
