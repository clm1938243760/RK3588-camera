#!/usr/bin/env bash
set -euo pipefail

URL="${DISPLAY_URL:-http://127.0.0.1:8080/display}"
WIDTH="${DISPLAY_WIDTH:-480}"
HEIGHT="${DISPLAY_HEIGHT:-320}"
export DISPLAY="${DISPLAY:-:0}"

for browser in chromium chromium-browser google-chrome; do
  if command -v "$browser" >/dev/null 2>&1; then
    exec "$browser" \
      --app="$URL" \
      --window-size="${WIDTH},${HEIGHT}" \
      --window-position=0,0 \
      --no-first-run \
      --disable-infobars \
      --disable-session-crashed-bubble \
      --check-for-update-interval=31536000
  fi
done

echo "No Chromium browser found. Open manually: $URL" >&2
exit 1
