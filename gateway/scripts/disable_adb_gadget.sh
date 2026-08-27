#!/usr/bin/env bash
set -euo pipefail

GADGET_DIR="${GADGET_DIR:-/sys/kernel/config/usb_gadget/rockchip}"
DISABLE_SERVICES="${DISABLE_SERVICES:-0}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run as root: sudo bash scripts/disable_adb_gadget.sh"
  exit 1
fi

echo "---- current adb/default gadget ----"
if [[ -d "$GADGET_DIR" ]]; then
  find "$GADGET_DIR" -maxdepth 3 -print
else
  echo "gadget directory not found: $GADGET_DIR"
fi

echo "---- stop possible adb services ----"
for service in adbd adb android-adbd android-tools-adbd; do
  if systemctl list-unit-files "${service}.service" >/dev/null 2>&1; then
    systemctl stop "${service}.service" 2>/dev/null || true
    if [[ "$DISABLE_SERVICES" = "1" ]]; then
      systemctl disable "${service}.service" 2>/dev/null || true
    fi
  fi
done

echo "---- unbind gadget UDC ----"
if [[ -f "$GADGET_DIR/UDC" ]]; then
  current="$(cat "$GADGET_DIR/UDC" 2>/dev/null || true)"
  echo "current UDC: ${current:-<empty>}"
  echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
  sleep 1
  echo "after unbind: $(cat "$GADGET_DIR/UDC" 2>/dev/null || true)"
else
  echo "UDC file not found under $GADGET_DIR"
fi

echo "---- remove adb function links ----"
if [[ -d "$GADGET_DIR/configs" ]]; then
  find "$GADGET_DIR/configs" -maxdepth 2 -type l -print -delete 2>/dev/null || true
fi

echo "---- leave function directories in place ----"
find "$GADGET_DIR/functions" -maxdepth 1 -type d -print 2>/dev/null || true

echo "ADB/default gadget released. Check with:"
echo "  find /sys/kernel/config/usb_gadget -maxdepth 3 -type f -name UDC -print -exec cat {} \\;"
echo "  ls -l /sys/class/udc"
