#!/bin/bash
set -euo pipefail

VERSION="${MEDIAMTX_VERSION:-v1.19.1}"
ARCHIVE="mediamtx_${VERSION}_linux_arm64.tar.gz"
URL="https://github.com/bluenviron/mediamtx/releases/download/${VERSION}/${ARCHIVE}"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Run with sudo: sudo $0"
  exit 1
fi

curl -fL "$URL" -o "$TMP/$ARCHIVE"
tar -xzf "$TMP/$ARCHIVE" -C "$TMP"
install -m 0755 "$TMP/mediamtx" /usr/local/bin/mediamtx
echo "Installed $(/usr/local/bin/mediamtx --version)"
