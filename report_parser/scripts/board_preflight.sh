#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
MANIFEST="$ROOT_DIR/runtime/manifest.json"

echo "== System =="
uname -a
python3 --version
free -h

echo "== NPU =="
ls -l /dev/rknpu* 2>/dev/null || true
find /sys/class/devfreq -maxdepth 1 -type d -name '*npu*' -print 2>/dev/null || true

echo "== PP-OCR =="
curl -fsS --max-time 5 http://127.0.0.1:5002/health || true
echo

echo "== RKLLM =="
find /opt /userdata /usr/local -iname 'librkllmrt.so' -o -iname '*.rkllm' 2>/dev/null | head -100 || true

echo "== Manifest =="
if [[ -f "$MANIFEST" ]]; then
  cat "$MANIFEST"
else
  echo "missing: $MANIFEST"
fi
