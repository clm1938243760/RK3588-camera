#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
CAMERA_DIR="${CAMERA_DIR:-/opt/rk3588_kvm}"
PARSER_DIR="${PARSER_DIR:-/opt/rk3588_report_parser}"
GATEWAY_DIR="${GATEWAY_DIR:-/opt/rk3588_gateway}"
RESTART=0
BOOTSTRAP_PYTHON=0

usage() {
  cat <<'EOF'
Usage: sudo bash install_stack.sh [--restart] [--bootstrap-python]

  --restart           Restart the active camera/OCR/report/display services.
  --bootstrap-python  Create parser/gateway virtual environments if missing.

USB gadget services and configfs are never modified by this installer.
EOF
}

die() {
  echo "ERROR: $*" >&2
  exit 1
}

while [[ $# -gt 0 ]]; do
  case "$1" in
    --restart)
      RESTART=1
      ;;
    --bootstrap-python)
      BOOTSTRAP_PYTHON=1
      ;;
    -h|--help)
      usage
      exit 0
      ;;
    *)
      usage >&2
      die "unknown option: $1"
      ;;
  esac
  shift
done

[[ "$(id -u)" -eq 0 ]] || die "run as root"
[[ -d "$ROOT_DIR/report_parser/src" ]] || die "report_parser component is missing"
[[ -d "$ROOT_DIR/gateway/src" ]] || die "gateway component is missing"
id linaro >/dev/null 2>&1 || die "the board service account 'linaro' is missing"

for command in install python3 rsync systemctl; do
  command -v "$command" >/dev/null 2>&1 || die "required command is missing: $command"
done

install -d -m 0755 "$CAMERA_DIR" "$PARSER_DIR" "$GATEWAY_DIR"

camera_files=(
  VERSION
  camera_mediamtx.yml
  camera_ocr_overlay.py
  camera_ocr_snapshots.sh
  camera_stream_mpp.sh
  camera_stream_watchdog.sh
  install_mediamtx.sh
  start_camera_preview.sh
  stop_camera_preview.sh
)

for file in "${camera_files[@]}"; do
  install -m 0644 "$ROOT_DIR/$file" "$CAMERA_DIR/$file"
done
chmod 0755 "$CAMERA_DIR/camera_ocr_snapshots.sh" "$CAMERA_DIR/camera_stream_mpp.sh" "$CAMERA_DIR/camera_stream_watchdog.sh" "$CAMERA_DIR/install_mediamtx.sh" "$CAMERA_DIR/start_camera_preview.sh" "$CAMERA_DIR/stop_camera_preview.sh"

install -d -m 0755 "$CAMERA_DIR/systemd"
rsync -a "$ROOT_DIR/systemd/" "$CAMERA_DIR/systemd/"

common_excludes=(
  '--exclude=.git/'
  '--exclude=__pycache__/'
  '--exclude=.pytest_cache/'
  '--exclude=.venv/'
  '--exclude=.venv-*/'
  '--exclude=node_modules/'
  '--exclude=build/'
  '--exclude=dist/'
  '--exclude=output/'
  --exclude='*.log'
  --exclude='*.sqlite*'
  --exclude='*.db'
  --exclude='*.key'
  --exclude='*.pem'
  --exclude='*.crt'
)

rsync -a "${common_excludes[@]}" "$ROOT_DIR/report_parser/" "$PARSER_DIR/"
rsync -a "${common_excludes[@]}" --exclude=config.yaml --exclude=ReportInfo.xml "$ROOT_DIR/gateway/" "$GATEWAY_DIR/"

if [[ ! -s "$GATEWAY_DIR/config.yaml" ]]; then
  install -m 0600 "$GATEWAY_DIR/config.example.yaml" "$GATEWAY_DIR/config.yaml"
  echo "Created $GATEWAY_DIR/config.yaml from the generic example."
fi

install -d -m 0700 /var/lib/rk3588-report-center /var/lib/rk3588-report-center/db /var/lib/rk3588-report-center/entry-captures /var/lib/rk3588-gateway /var/lib/rk3588-gateway/device
install -d -o linaro -g linaro -m 0700 /var/lib/rk3588-report-parser

if [[ ! -s /var/lib/rk3588-report-parser/active_identifier_rules.json ]]; then
  install -o linaro -g linaro -m 0600 "$PARSER_DIR/runtime/active_identifier_rules.json" /var/lib/rk3588-report-parser/active_identifier_rules.json
fi

bootstrap_venv() {
  local app_dir="$1"
  local venv_name="$2"
  local requirements="$3"
  local venv_path="$app_dir/$venv_name"

  if [[ -x "$venv_path/bin/python" ]]; then
    return
  fi
  [[ "$BOOTSTRAP_PYTHON" -eq 1 ]] || die "$venv_path is missing; rerun with --bootstrap-python"

  python3 -m venv --system-site-packages "$venv_path"
  "$venv_path/bin/pip" install --upgrade "pip<24" "setuptools<68" wheel
  "$venv_path/bin/pip" install -r "$app_dir/$requirements"
  "$venv_path/bin/pip" install -e "$app_dir"
}

bootstrap_venv "$PARSER_DIR" .venv-camera requirements-camera-trigger-board.txt
bootstrap_venv "$GATEWAY_DIR" .venv requirements.txt

chmod 0755 "$GATEWAY_DIR/scripts/fb_status.py"
find "$GATEWAY_DIR/scripts" -maxdepth 1 -type f -name '*.sh' -exec chmod 0755 {} +
find "$PARSER_DIR/scripts" -maxdepth 1 -type f -name '*.sh' -exec chmod 0755 {} +

camera_units=(
  rk3588-camera-mediamtx.service
  rk3588-camera-stream.service
  rk3588-camera-ocr-snapshots.service
  rk3588-camera-ocr-overlay.service
)
parser_units=(
  rk3588-report-camera-trigger.service
  rk3588-camera-report-center-forwarder.service
)
gateway_units=(
  rk3588-ppocr.service
  rk3588-report-center.service
  rk3588-fb-status.service
)

for unit in "${camera_units[@]}"; do
  install -m 0644 "$ROOT_DIR/systemd/$unit" "/etc/systemd/system/$unit"
done
for unit in "${parser_units[@]}"; do
  install -m 0644 "$PARSER_DIR/systemd/$unit" "/etc/systemd/system/$unit"
done
for unit in "${gateway_units[@]}"; do
  install -m 0644 "$GATEWAY_DIR/systemd/$unit" "/etc/systemd/system/$unit"
done

active_units=(
  rk3588-camera-mediamtx.service
  rk3588-camera-stream.service
  rk3588-camera-ocr-snapshots.service
  rk3588-ppocr.service
  rk3588-report-camera-trigger.service
  rk3588-report-center.service
  rk3588-camera-report-center-forwarder.service
  rk3588-camera-ocr-overlay.service
  rk3588-fb-status.service
)

systemctl daemon-reload
systemctl enable "${active_units[@]}"

if [[ ! -x /usr/local/bin/mediamtx ]]; then
  echo "WARNING: /usr/local/bin/mediamtx is missing." >&2
  echo "Install it with: sudo $CAMERA_DIR/install_mediamtx.sh" >&2
fi

if [[ "$RESTART" -eq 1 ]]; then
  restart_order=(
    rk3588-ppocr.service
    rk3588-report-center.service
    rk3588-camera-mediamtx.service
    rk3588-camera-stream.service
    rk3588-camera-ocr-snapshots.service
    rk3588-report-camera-trigger.service
    rk3588-camera-report-center-forwarder.service
    rk3588-camera-ocr-overlay.service
    rk3588-fb-status.service
  )
  for unit in "${restart_order[@]}"; do
    systemctl restart "$unit"
  done
  echo "Active stack restarted."
else
  echo "Files and units installed. Services were not restarted."
  echo "Review $GATEWAY_DIR/config.yaml, then run:"
  echo "  sudo bash $ROOT_DIR/install_stack.sh --restart"
fi

echo "Camera: $CAMERA_DIR"
echo "Parser: $PARSER_DIR"
echo "Gateway: $GATEWAY_DIR"
echo "USB gadget state was not modified."
