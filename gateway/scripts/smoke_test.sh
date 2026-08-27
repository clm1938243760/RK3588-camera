#!/usr/bin/env bash
set -euo pipefail

HOST="${HOST:-127.0.0.1}"
PORT="${PORT:-8080}"

curl -fsS "http://$HOST:$PORT/health"
echo
curl -fsS "http://$HOST:$PORT/status"
echo
curl -fsS -X POST "http://$HOST:$PORT/events" \
  -H "Content-Type: application/json" \
  -d '{"type":"smoke.test","payload":{"message":"hello from rk3588"}}'
echo
