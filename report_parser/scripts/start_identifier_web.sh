#!/bin/sh
set -eu

ROOT="$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)"
PYTHON="${PYTHON:-$ROOT/.venv/bin/python}"
CONFIG="${CONFIG:-$ROOT/config.edge.example.json}"
HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8020}"

export PYTHONPATH="$ROOT/src"
exec "$PYTHON" -m rk3588_report_parser.web_server \
  --config "$CONFIG" \
  --host "$HOST" \
  --port "$PORT" \
  "$@"
