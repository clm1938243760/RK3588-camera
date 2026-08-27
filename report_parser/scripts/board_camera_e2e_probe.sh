#!/usr/bin/env bash
set -euo pipefail

ROOT="${REPORT_PARSER_ROOT:-/opt/rk3588_report_parser}"
PYTHON_BIN="${REPORT_CAMERA_PYTHON:-$ROOT/.venv-camera/bin/python}"
SOURCE_IMAGE="${1:?usage: board_camera_e2e_probe.sh IMAGE [EXPECTED_LENGTH]}"
EXPECTED_LENGTH="${2:-0}"
PROBE_ROOT="${REPORT_CAMERA_PROBE_DIR:-/tmp/rk3588_report_camera_probe}"
FRAME_PREFIX="$PROBE_ROOT/frame"
STATUS_FILE="$PROBE_ROOT/status.json"
RESULT_FILE="$PROBE_ROOT/result.json"
WRITER_PID=""

cleanup() {
  if [[ -n "$WRITER_PID" ]]; then
    kill "$WRITER_PID" 2>/dev/null || true
    wait "$WRITER_PID" 2>/dev/null || true
  fi
  rm -rf -- "$PROBE_ROOT"
}
trap cleanup EXIT INT TERM

test -r "$SOURCE_IMAGE"
test -x "$PYTHON_BIN"
mkdir -p "$PROBE_ROOT"
chmod 700 "$PROBE_ROOT"

(
  for index in $(seq 0 79); do
    slot=$((index % 4))
    temporary="$FRAME_PREFIX-$slot.jpg.tmp"
    target="$FRAME_PREFIX-$slot.jpg"
    cp -- "$SOURCE_IMAGE" "$temporary"
    mv -f -- "$temporary" "$target"
    sleep 0.2
  done
) &
WRITER_PID=$!

set +e
timeout 45s "$PYTHON_BIN" "$ROOT/scripts/camera_paper_trigger.py" \
  --frame-glob "$FRAME_PREFIX-*.jpg" \
  --paper-model "$ROOT/runtime/docaligner/lcnet050_p_multi_decoder_l3_d64_256_fp32.onnx" \
  --detector-backend onnxruntime \
  --ocr-endpoint http://127.0.0.1:5002/ocr \
  --config "$ROOT/config.rk3588.ocr_only.json" \
  --rules-file /var/lib/rk3588-report-parser/active_identifier_rules.json \
  --burst-frames 5 \
  --ocr-document-long-side 3200 \
  --status-file "$STATUS_FILE" \
  --result-file "$RESULT_FILE" \
  --once-after-verification \
  --max-frames 80 \
  >/dev/null
TRIGGER_EXIT=$?
set -e

kill "$WRITER_PID" 2>/dev/null || true
wait "$WRITER_PID" 2>/dev/null || true
WRITER_PID=""

"$PYTHON_BIN" - "$STATUS_FILE" "$RESULT_FILE" "$EXPECTED_LENGTH" "$TRIGGER_EXIT" <<'PY'
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
result_path = Path(sys.argv[2])
expected_length = int(sys.argv[3])
trigger_exit = int(sys.argv[4])

status = json.loads(status_path.read_text(encoding="utf-8")) if status_path.is_file() else {}
result = json.loads(result_path.read_text(encoding="utf-8")) if result_path.is_file() else {}
identifier = result.get("identifier")
identifier_length = len(identifier) if isinstance(identifier, str) else 0
verification = status.get("verification") if isinstance(status.get("verification"), dict) else {}
payload = {
    "trigger_exit": trigger_exit,
    "capture_stage": status.get("capture_stage"),
    "verification_status": verification.get("status"),
    "verification_reason": verification.get("reason"),
    "attempt": verification.get("attempt"),
    "processed_frames": status.get("processed_frames"),
    "identifier_available": isinstance(identifier, str) and bool(identifier),
    "identifier_length": identifier_length,
    "expected_length_match": expected_length == 0 or identifier_length == expected_length,
    "identifier_disclosed": False,
}
print(json.dumps(payload, ensure_ascii=False, indent=2))
if (
    trigger_exit != 0
    or payload["capture_stage"] != "verified"
    or payload["verification_status"] != "accepted"
    or not payload["identifier_available"]
    or not payload["expected_length_match"]
):
    raise SystemExit(1)
PY
