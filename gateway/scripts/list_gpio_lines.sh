#!/usr/bin/env bash
set -euo pipefail

if ! command -v gpioinfo >/dev/null 2>&1; then
  echo "gpioinfo not found. Install with: apt install -y gpiod"
  exit 1
fi

echo "GPIO chips:"
gpiodetect
echo
echo "GPIO lines:"
gpioinfo
