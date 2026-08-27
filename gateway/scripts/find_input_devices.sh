#!/usr/bin/env bash
set -euo pipefail

echo "USB devices:"
lsusb || true

echo
echo "Input devices by id:"
ls -l /dev/input/by-id/ || true

echo
echo "Kernel input names:"
grep -H . /sys/class/input/event*/device/name 2>/dev/null || true
