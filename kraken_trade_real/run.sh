#!/bin/bash
set -euo pipefail

mkdir -p /data/logs /data/state

exec python3 -m app.main
