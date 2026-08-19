#!/bin/bash
set -euo pipefail

MEDIAMTX_PID_FILE="${CAMERA_MEDIAMTX_PID_FILE:-/tmp/rk3588_camera_mediamtx.pid}"
STREAM_PID_FILE="${CAMERA_STREAM_PID_FILE:-/tmp/rk3588_camera_stream.pid}"
WORKER_PID_FILE="${CAMERA_WORKER_PID_FILE:-/tmp/rk3588_camera_stream_worker.pid}"

stop_pid_file() {
  local pid_path="$1"
  [[ -f "$pid_path" ]] || return 0
  local pid
  pid="$(cat "$pid_path" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    kill "$pid" 2>/dev/null || true
    for _ in $(seq 1 30); do
      kill -0 "$pid" 2>/dev/null || break
      sleep 0.1
    done
  fi
  rm -f "$pid_path"
}

stop_pid_file "$STREAM_PID_FILE"
stop_pid_file "$WORKER_PID_FILE"
stop_pid_file "$MEDIAMTX_PID_FILE"
echo "CSI camera preview stopped."
