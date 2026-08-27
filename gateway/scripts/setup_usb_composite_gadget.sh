#!/usr/bin/env bash
set -euo pipefail

CONFIGFS="${CONFIGFS:-/sys/kernel/config}"
C0_GADGET_DIR="${C0_GADGET_DIR:-/sys/kernel/config/usb_gadget/rk3588_c0_hid_printer}"
C1_GADGET_DIR="${C1_GADGET_DIR:-/sys/kernel/config/usb_gadget/rk3588_c1_msc}"
C0_UDC="${C0_UDC:-fc000000.usb}"
C1_UDC="${C1_UDC:-fc400000.usb}"
MSC_IMAGE="${MSC_IMAGE:-/var/lib/rk3588-gateway/msc/ums_shared.img}"
MSC_SIZE_MB="${MSC_SIZE_MB:-64}"
MSC_LABEL="${MSC_LABEL:-RK3588MSC}"

modprobe libcomposite 2>/dev/null || true
modprobe usb_f_hid 2>/dev/null || true
modprobe usb_f_printer 2>/dev/null || true
modprobe usb_f_mass_storage 2>/dev/null || true
mountpoint -q "$CONFIGFS" || mount -t configfs none "$CONFIGFS"

systemctl stop usbdevice.service 2>/dev/null || true
pkill adbd 2>/dev/null || true

require_udc() {
  local udc="$1"
  if [[ ! -e "/sys/class/udc/$udc" ]]; then
    echo "Required UDC not found: $udc"
    echo "Available UDCs: $(ls /sys/class/udc 2>/dev/null | tr '\n' ' ')"
    exit 1
  fi
}

unbind_gadget() {
  local gadget="$1"
  if [[ -f "$gadget/UDC" ]]; then
    echo "" > "$gadget/UDC" 2>/dev/null || true
    sleep 1
  fi
}

remove_gadget() {
  local gadget="$1"
  [[ -d "$gadget" ]] || return 0

  unbind_gadget "$gadget"
  clear_config_links "$gadget"
  if [[ -d "$gadget/functions" ]]; then
    for function_dir in "$gadget"/functions/*; do
      [[ -e "$function_dir" ]] || continue
      rmdir "$function_dir" 2>/dev/null || true
    done
  fi
  find "$gadget/configs" "$gadget/strings" -depth -type d -exec rmdir {} \; 2>/dev/null || true
  rmdir "$gadget/functions" 2>/dev/null || true
  rmdir "$gadget" 2>/dev/null || true
}

clear_config_links() {
  local gadget="$1"
  if [[ -d "$gadget/configs" ]]; then
    find "$gadget/configs" -type l -exec rm -f {} \; 2>/dev/null || true
  fi
}

write_device_ids() {
  local gadget="$1"
  local product_id="$2"
  local product="$3"
  local serial="$4"

  echo 0x2207 > "$gadget/idVendor"
  echo "$product_id" > "$gadget/idProduct"
  echo 0x0200 > "$gadget/bcdUSB"
  echo 0x0100 > "$gadget/bcdDevice"
  echo 0x00 > "$gadget/bDeviceClass"
  echo 0x00 > "$gadget/bDeviceSubClass"
  echo 0x00 > "$gadget/bDeviceProtocol"

  mkdir -p "$gadget/strings/0x409"
  echo "$serial" > "$gadget/strings/0x409/serialnumber"
  echo "RK3588" > "$gadget/strings/0x409/manufacturer"
  echo "$product" > "$gadget/strings/0x409/product"
}

prepare_config() {
  local gadget="$1"
  local config="$gadget/configs/c.1"
  local label="$2"
  local max_power="$3"

  mkdir -p "$config/strings/0x409"
  echo "$label" > "$config/strings/0x409/configuration"
  echo "$max_power" > "$config/MaxPower"
}

prepare_msc_image() {
  mkdir -p "$(dirname "$MSC_IMAGE")"
  if [[ ! -s "$MSC_IMAGE" ]]; then
    dd if=/dev/zero of="$MSC_IMAGE" bs=1M count="$MSC_SIZE_MB"
    if command -v mkfs.vfat >/dev/null 2>&1; then
      mkfs.vfat -n "$MSC_LABEL" "$MSC_IMAGE"
    elif command -v mkfs.fat >/dev/null 2>&1; then
      mkfs.fat -n "$MSC_LABEL" "$MSC_IMAGE"
    else
      echo "mkfs.vfat not found. Install dosfstools first."
      exit 1
    fi
  fi
}

setup_c0_hid_printer() {
  local gadget="$C0_GADGET_DIR"
  local config="$gadget/configs/c.1"
  local functions="$gadget/functions"

  mkdir -p "$gadget"
  unbind_gadget "$gadget"
  clear_config_links "$gadget"

  write_device_ids "$gadget" 0x3588 "RK3588 Keyboard Mouse Printer" "RK3588C0KMP001"
  prepare_config "$gadget" "C0 keyboard mouse printer" 120

  mkdir -p "$functions/hid.keyboard"
  echo 1 > "$functions/hid.keyboard/protocol"
  echo 1 > "$functions/hid.keyboard/subclass"
  echo 8 > "$functions/hid.keyboard/report_length"
  printf '\x05\x01\x09\x06\xa1\x01\x05\x07\x19\xe0\x29\xe7\x15\x00\x25\x01\x75\x01\x95\x08\x81\x02\x95\x01\x75\x08\x81\x03\x95\x05\x75\x01\x05\x08\x19\x01\x29\x05\x91\x02\x95\x01\x75\x03\x91\x03\x95\x06\x75\x08\x15\x00\x25\x65\x05\x07\x19\x00\x29\x65\x81\x00\xc0' > "$functions/hid.keyboard/report_desc"

  mkdir -p "$functions/hid.mouse"
  echo 2 > "$functions/hid.mouse/protocol"
  echo 1 > "$functions/hid.mouse/subclass"
  echo 5 > "$functions/hid.mouse/report_length"
  printf '\x05\x01\x09\x02\xa1\x01\x09\x01\xa1\x00\x05\x09\x19\x01\x29\x03\x15\x00\x25\x01\x95\x03\x75\x01\x81\x02\x95\x01\x75\x05\x81\x03\x05\x01\x09\x30\x09\x31\x16\x00\x00\x26\xff\x7f\x36\x00\x00\x46\xff\x7f\x75\x10\x95\x02\x81\x02\xc0\xc0' > "$functions/hid.mouse/report_desc"

  mkdir -p "$functions/printer.usb0"
  echo 4 > "$functions/printer.usb0/q_len"
  echo "MFG:RK3588;MDL:Virtual Printer;DES:RK3588 Virtual Printer;CMD:POSTSCRIPT,RAW;CLS:PRINTER;" > "$functions/printer.usb0/pnp_string"

  # Keep the function order verified on RK3588: keyboard, mouse, then printer.
  ln -s "$functions/hid.keyboard" "$config/f1"
  ln -s "$functions/hid.mouse" "$config/f2"
  ln -s "$functions/printer.usb0" "$config/f3"

  echo "$C0_UDC" > "$gadget/UDC"
}

setup_c1_msc() {
  local gadget="$C1_GADGET_DIR"
  local config="$gadget/configs/c.1"
  local functions="$gadget/functions"
  local msc="$functions/mass_storage.usb0"

  prepare_msc_image
  mkdir -p "$gadget"
  unbind_gadget "$gadget"
  clear_config_links "$gadget"

  write_device_ids "$gadget" 0x3589 "RK3588 C1 Mass Storage" "RK3588C1MSC001"
  prepare_config "$gadget" "C1 mass storage" 250

  mkdir -p "$msc"
  echo "" > "$msc/lun.0/file" 2>/dev/null || true
  echo 0 > "$msc/stall" 2>/dev/null || true
  echo 1 > "$msc/lun.0/removable"
  echo 0 > "$msc/lun.0/ro"
  echo 1 > "$msc/lun.0/nofua" 2>/dev/null || true
  echo "$MSC_IMAGE" > "$msc/lun.0/file"

  ln -s "$msc" "$config/f1"
  echo "$C1_UDC" > "$gadget/UDC"
}

require_udc "$C0_UDC"
require_udc "$C1_UDC"

# Remove bring-up test gadgets first so /dev/hidg0, /dev/hidg1 and
# /dev/g_printer0 are allocated predictably for the production gadget.
remove_gadget "/sys/kernel/config/usb_gadget/test_c0_kmp"
remove_gadget "/sys/kernel/config/usb_gadget/test_c1_msc"
remove_gadget "$C0_GADGET_DIR"
remove_gadget "$C1_GADGET_DIR"

setup_c0_hid_printer
setup_c1_msc
sleep 2

echo "C0 gadget: $C0_GADGET_DIR UDC=$C0_UDC state=$(cat /sys/class/udc/$C0_UDC/state 2>/dev/null || true) speed=$(cat /sys/class/udc/$C0_UDC/current_speed 2>/dev/null || true)"
echo "C1 gadget: $C1_GADGET_DIR UDC=$C1_UDC state=$(cat /sys/class/udc/$C1_UDC/state 2>/dev/null || true) speed=$(cat /sys/class/udc/$C1_UDC/current_speed 2>/dev/null || true)"
ls -l /dev/g_printer0 /dev/hidg0 /dev/hidg1 2>/dev/null || true
echo "mass storage backing file: $MSC_IMAGE"
