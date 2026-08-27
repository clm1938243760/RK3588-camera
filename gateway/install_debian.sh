#!/usr/bin/env bash
set -euo pipefail

APP_DIR="${APP_DIR:-/opt/rk3588_gateway}"
DATA_DIR="${DATA_DIR:-/var/lib/rk3588-gateway}"
SERVICE_NAME="rk3588-gateway.service"
GADGET_SERVICE_NAME="rk3588-usb-printer-gadget.service"
FB_STATUS_SERVICE_NAME="rk3588-fb-status.service"
VISION_SERVICE_NAME="rk3588-ppocr.service"
REPORT_CENTER_SERVICE_NAME="rk3588-report-center.service"
ENABLE_FB_STATUS="${ENABLE_FB_STATUS:-0}"
ENABLE_REPORT_CENTER_SHADOW="${ENABLE_REPORT_CENTER_SHADOW:-1}"

if [[ "$(id -u)" -ne 0 ]]; then
  echo "Please run as root: sudo bash install_debian.sh"
  exit 1
fi

apt update
apt install -y \
  python3 python3-venv python3-pip python3-dev build-essential \
  cups cups-filters ghostscript printer-driver-hpcups hplip \
  libreoffice \
  rsync curl nano openssh-client sshpass dosfstools util-linux gpiod openssl \
  fonts-wqy-microhei libjpeg-dev zlib1g-dev libfreetype6-dev

mkdir -p "$APP_DIR" "$DATA_DIR" "$DATA_DIR/device" /var/lib/rk3588-report-center
chmod 0700 /var/lib/rk3588-report-center
rsync -a --delete \
  --exclude ".git" \
  --exclude ".venv" \
  --exclude "config.yaml" \
  ./ "$APP_DIR/"

if [[ -s "$APP_DIR/ReportInfo.xml" ]]; then
  install -m 0644 "$APP_DIR/ReportInfo.xml" "$DATA_DIR/device/ReportInfo.xml"
fi
if [[ -d "$APP_DIR/assets/icon_match" ]]; then
  icon_templates=("$APP_DIR"/assets/icon_match/*.jpg)
  install -d -m 0755 /userdata/aidemo/icon_match
  if [[ -e "${icon_templates[0]}" ]]; then
    install -m 0644 "${icon_templates[@]}" /userdata/aidemo/icon_match/
  fi
fi

python3 -m venv "$APP_DIR/.venv"
"$APP_DIR/.venv/bin/pip" install --upgrade "pip<24" "setuptools<68" wheel
"$APP_DIR/.venv/bin/pip" install -r "$APP_DIR/requirements.txt"
"$APP_DIR/.venv/bin/pip" install -e "$APP_DIR"

if [[ ! -s "$APP_DIR/config.yaml" ]]; then
  cp "$APP_DIR/config.example.yaml" "$APP_DIR/config.yaml"
fi
chmod +x "$APP_DIR/scripts/setup_usb_composite_gadget.sh"
chmod +x "$APP_DIR/scripts/setup_usb_printer_gadget.sh" 2>/dev/null || true
chmod +x "$APP_DIR/scripts/check_kernel_features.sh"
chmod +x "$APP_DIR/scripts/disable_adb_gadget.sh"
chmod +x "$APP_DIR/scripts/list_gpio_lines.sh"
chmod +x "$APP_DIR/scripts/configure_gpio_buttons.py"
chmod +x "$APP_DIR/scripts/fb_status.py"
chmod +x "$APP_DIR/scripts/ppocr_rknn_server.py"

cp "$APP_DIR/systemd/$GADGET_SERVICE_NAME" "/etc/systemd/system/$GADGET_SERVICE_NAME"
cp "$APP_DIR/systemd/$SERVICE_NAME" "/etc/systemd/system/$SERVICE_NAME"
cp "$APP_DIR/systemd/$FB_STATUS_SERVICE_NAME" "/etc/systemd/system/$FB_STATUS_SERVICE_NAME"
cp "$APP_DIR/systemd/$VISION_SERVICE_NAME" "/etc/systemd/system/$VISION_SERVICE_NAME"
cp "$APP_DIR/systemd/$REPORT_CENTER_SERVICE_NAME" "/etc/systemd/system/$REPORT_CENTER_SERVICE_NAME"

systemctl daemon-reload
systemctl disable --now usbdevice.service 2>/dev/null || true
pkill adbd 2>/dev/null || true
systemctl disable --now rk3588-gateway.service 2>/dev/null || true
systemctl disable --now rk3588-usb-printer-gadget.service 2>/dev/null || true
systemctl disable --now rk3588-usb-hid-gadget.service 2>/dev/null || true
systemctl disable --now rk3588-fb-status.service 2>/dev/null || true
systemctl disable --now rk3588-ppocr.service 2>/dev/null || true
systemctl disable --now "$REPORT_CENTER_SERVICE_NAME" 2>/dev/null || true

systemctl enable "$GADGET_SERVICE_NAME" "$VISION_SERVICE_NAME" "$SERVICE_NAME"
systemctl restart cups || true
systemctl restart "$GADGET_SERVICE_NAME" || true
systemctl restart "$VISION_SERVICE_NAME" || true
systemctl restart "$SERVICE_NAME"
if [[ "$ENABLE_REPORT_CENTER_SHADOW" = "1" ]]; then
  systemctl enable "$REPORT_CENTER_SERVICE_NAME"
  systemctl restart "$REPORT_CENTER_SERVICE_NAME"
else
  systemctl disable --now "$REPORT_CENTER_SERVICE_NAME" 2>/dev/null || true
fi
if [[ "$ENABLE_FB_STATUS" = "1" ]]; then
  systemctl enable "$FB_STATUS_SERVICE_NAME"
  systemctl restart "$FB_STATUS_SERVICE_NAME" || true
else
  systemctl disable --now "$FB_STATUS_SERVICE_NAME" 2>/dev/null || true
fi

echo "Installed to $APP_DIR"
echo "Data directory: $DATA_DIR"
echo "Edit $APP_DIR/config.yaml for API/printer details, then run:"
echo "  sudo systemctl restart $GADGET_SERVICE_NAME $VISION_SERVICE_NAME $SERVICE_NAME"
echo "  sudo journalctl -u $SERVICE_NAME -f"
echo "Report center shadow service: $REPORT_CENTER_SERVICE_NAME (HTTPS port 8443)"
echo "  sudo journalctl -u $REPORT_CENTER_SERVICE_NAME -f"
