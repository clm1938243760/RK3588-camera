#!/usr/bin/env bash
set -euo pipefail

GADGET_NAME="${GADGET_NAME:-rockchip}"
SERIAL="${SERIAL:-RK3588BRIDGE001}"
MANUFACTURER="${MANUFACTURER:-RK3588}"
PRODUCT="${PRODUCT:-RK3588 HID Keyboard Mouse Printer Bridge}"
ENABLE_PRINTER="${ENABLE_PRINTER:-1}"
ENABLE_HID="${ENABLE_HID:-1}"
CONFIGFS="/sys/kernel/config"
GADGET_DIR="$CONFIGFS/usb_gadget/$GADGET_NAME"

modprobe libcomposite 2>/dev/null || true
mountpoint -q "$CONFIGFS" || mount -t configfs none "$CONFIGFS"

UDC="${UDC:-}"
if [[ -z "$UDC" ]]; then
  UDC="$(ls /sys/class/udc 2>/dev/null | head -n 1 || true)"
fi

if [[ -z "$UDC" ]]; then
  echo "No UDC found under /sys/class/udc"
  echo "Check that the Rockchip OTG USB port is in device/peripheral mode."
  exit 1
fi

if [[ -d "$GADGET_DIR" ]]; then
  if [[ -f "$GADGET_DIR/UDC" ]]; then
    echo "" > "$GADGET_DIR/UDC" 2>/dev/null || true
    sleep 1
  fi
  find "$GADGET_DIR/configs" -maxdepth 2 -type l -exec rm -f {} \; 2>/dev/null || true
fi

mkdir -p "$GADGET_DIR"
cd "$GADGET_DIR"

echo 0x2207 > idVendor
echo 0x3588 > idProduct
echo 0x0100 > bcdDevice
echo 0x0200 > bcdUSB

mkdir -p strings/0x409
echo "$SERIAL" > strings/0x409/serialnumber
echo "$MANUFACTURER" > strings/0x409/manufacturer
echo "$PRODUCT" > strings/0x409/product

CONFIG_DIR=""
if [[ -d configs/b.1 ]]; then
  CONFIG_DIR="configs/b.1"
elif [[ -d configs/c.1 ]]; then
  CONFIG_DIR="configs/c.1"
else
  mkdir -p configs/b.1
  CONFIG_DIR="configs/b.1"
fi

mkdir -p "$CONFIG_DIR/strings/0x409"
echo "HID Keyboard + HID Mouse + USB Printer" > "$CONFIG_DIR/strings/0x409/configuration"
echo 120 > "$CONFIG_DIR/MaxPower"

if [[ "$ENABLE_HID" = "1" ]]; then
  mkdir -p functions/hid.usb0
  echo 1 > functions/hid.usb0/protocol
  echo 1 > functions/hid.usb0/subclass
  echo 8 > functions/hid.usb0/report_length
  printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' > functions/hid.usb0/report_desc

  mkdir -p functions/hid.usb1
  echo 2 > functions/hid.usb1/protocol
  echo 1 > functions/hid.usb1/subclass
  echo 5 > functions/hid.usb1/report_length
  printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x16\x00\x00\x26\xff\x7f\x36\x00\x00\x46\xff\x7f\x75\x10\x95\x02\x81\x02\xc0\xc0' > functions/hid.usb1/report_desc

  ln -sf functions/hid.usb0 "$CONFIG_DIR/f_keyboard"
  ln -sf functions/hid.usb1 "$CONFIG_DIR/f_mouse"
fi

if [[ "$ENABLE_PRINTER" = "1" ]]; then
  if mkdir -p functions/printer.usb0 2>/dev/null; then
    echo "MFG:RK3588;MDL:Virtual Printer;CLS:PRINTER;" > functions/printer.usb0/pnp_string 2>/dev/null || true
    echo 10 > functions/printer.usb0/q_len 2>/dev/null || true
    ln -sf functions/printer.usb0 "$CONFIG_DIR/f_printer"
  else
    echo "Printer gadget function is not available in this kernel."
  fi
fi

echo "$UDC" > UDC
sleep 1

ls -l /dev/hidg0 /dev/hidg1 /dev/g_printer0 2>/dev/null || true
echo "USB HID/printer gadget enabled on UDC=$UDC"
