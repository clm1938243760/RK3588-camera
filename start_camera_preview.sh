#!/bin/bash
set -euo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
MEDIAMTX_BIN="${CAMERA_MEDIAMTX_BIN:-/usr/local/bin/mediamtx}"
MEDIAMTX_PID_FILE="${CAMERA_MEDIAMTX_PID_FILE:-/tmp/rk3588_camera_mediamtx.pid}"
STREAM_PID_FILE="${CAMERA_STREAM_PID_FILE:-/tmp/rk3588_camera_stream.pid}"
MEDIAMTX_LOG_FILE="${CAMERA_MEDIAMTX_LOG_FILE:-/tmp/rk3588_camera_mediamtx.log}"
STREAM_LOG_FILE="${CAMERA_STREAM_LOG_FILE:-/tmp/rk3588_camera_stream.log}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run with sudo: sudo $ROOT/start_camera_preview.sh"
  exit 1
fi

for dev in /dev/video23; do
  [[ -e "$dev" ]] || { echo "Missing $dev"; exit 1; }
done
[[ -x "$MEDIAMTX_BIN" ]] || { echo "MediaMTX not found: $MEDIAMTX_BIN"; exit 1; }

for pid_path in "$MEDIAMTX_PID_FILE" "$STREAM_PID_FILE"; do
  [[ -f "$pid_path" ]] || continue
  pid="$(cat "$pid_path" 2>/dev/null || true)"
  if [[ "$pid" =~ ^[0-9]+$ ]] && kill -0 "$pid" 2>/dev/null; then
    echo "CSI camera preview already running, pid=$pid"
    exit 0
  fi
  rm -f "$pid_path"
done

: >"$MEDIAMTX_LOG_FILE"
nohup "$MEDIAMTX_BIN" "$ROOT/camera_mediamtx.yml" >>"$MEDIAMTX_LOG_FILE" 2>&1 &
echo "$!" >"$MEDIAMTX_PID_FILE"

for _ in $(seq 1 20); do
  if curl -fsS --max-time 1 http://127.0.0.1:9998/v3/paths/list >/dev/null 2>&1; then
    break
  fi
  sleep 0.1
done
if ! curl -fsS --max-time 1 http://127.0.0.1:9998/v3/paths/list >/dev/null 2>&1; then
  echo "Camera MediaMTX did not start; see $MEDIAMTX_LOG_FILE"
  kill "$(cat "$MEDIAMTX_PID_FILE")" 2>/dev/null || true
  rm -f "$MEDIAMTX_PID_FILE"
  exit 1
fi

: >"$STREAM_LOG_FILE"
nohup "$ROOT/camera_stream_watchdog.sh" >>"$STREAM_LOG_FILE" 2>&1 &
echo "$!" >"$STREAM_PID_FILE"

IP="$(hostname -I | awk '{print $1}')"
echo "CSI camera preview started."
echo "WebRTC: http://${IP}:8891/camera"
echo "RTSP:   rtsp://${IP}:8555/camera"
echo "Logs:   $STREAM_LOG_FILE"
