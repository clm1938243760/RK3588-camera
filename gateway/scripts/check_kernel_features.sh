#!/usr/bin/env bash
set -u

PASS=0
WARN=0
FAIL=0

say() {
  printf '%s\n' "$*"
}

ok() {
  PASS=$((PASS + 1))
  printf '[OK]   %s\n' "$*"
}

warn() {
  WARN=$((WARN + 1))
  printf '[WARN] %s\n' "$*"
}

fail() {
  FAIL=$((FAIL + 1))
  printf '[FAIL] %s\n' "$*"
}

kernel_config_file=""

find_kernel_config() {
  local release
  release="$(uname -r)"
  if [ -r /proc/config.gz ]; then
    kernel_config_file="/proc/config.gz"
    return
  fi
  if [ -r "/boot/config-$release" ]; then
    kernel_config_file="/boot/config-$release"
    return
  fi
  if [ -r "/lib/modules/$release/build/.config" ]; then
    kernel_config_file="/lib/modules/$release/build/.config"
    return
  fi
}

config_value() {
  local symbol="$1"
  if [ -z "$kernel_config_file" ]; then
    return 2
  fi
  if [ "$kernel_config_file" = "/proc/config.gz" ]; then
    zcat /proc/config.gz 2>/dev/null | grep -E "^${symbol}=(y|m)$" | tail -n 1 | cut -d= -f2
  else
    grep -E "^${symbol}=(y|m)$" "$kernel_config_file" 2>/dev/null | tail -n 1 | cut -d= -f2
  fi
}

config_line() {
  local symbol="$1"
  if [ -z "$kernel_config_file" ]; then
    return 2
  fi
  if [ "$kernel_config_file" = "/proc/config.gz" ]; then
    zcat /proc/config.gz 2>/dev/null | grep -E "^${symbol}=|^# ${symbol} is not set" | tail -n 1
  else
    grep -E "^${symbol}=|^# ${symbol} is not set" "$kernel_config_file" 2>/dev/null | tail -n 1
  fi
}

check_config_any() {
  local label="$1"
  shift
  local symbol value seen
  seen=""
  for symbol in "$@"; do
    value="$(config_value "$symbol" || true)"
    if [ "$value" = "y" ] || [ "$value" = "m" ]; then
      ok "$label: $symbol=$value"
      return 0
    fi
    seen="$seen $symbol"
  done
  if [ -z "$kernel_config_file" ]; then
    warn "$label: kernel .config not readable; expected one of:$seen"
    return 1
  fi
  fail "$label: missing expected config:$seen"
  return 1
}

check_config_disabled() {
  local label="$1"
  local symbol="$2"
  local line
  if [ -z "$kernel_config_file" ]; then
    warn "$label: kernel .config not readable; expected $symbol disabled"
    return 1
  fi
  line="$(config_line "$symbol" || true)"
  if [ "$line" = "${symbol}=y" ] || [ "$line" = "${symbol}=m" ]; then
    fail "$label: $line must be disabled for RK3588 dual UDC stability"
    return 1
  fi
  ok "$label: ${line:-$symbol absent}"
  return 0
}

check_module_or_builtin() {
  local module="$1"
  local description="$2"
  if lsmod 2>/dev/null | awk '{print $1}' | grep -qx "$module"; then
    ok "$description: module loaded ($module)"
    return 0
  fi
  if modprobe -n "$module" >/dev/null 2>&1; then
    ok "$description: module available ($module)"
    return 0
  fi
  warn "$description: modprobe cannot find $module; it may still be built into the kernel"
  return 1
}

check_path() {
  local path="$1"
  local description="$2"
  if [ -e "$path" ]; then
    ok "$description: $path"
  else
    fail "$description missing: $path"
  fi
}

say "=== RK3588 gateway kernel feature check ==="
say "kernel: $(uname -a)"

find_kernel_config
if [ -n "$kernel_config_file" ]; then
  ok "kernel config readable: $kernel_config_file"
else
  warn "kernel config not found at /proc/config.gz, /boot/config-\$(uname -r), or /lib/modules/\$(uname -r)/build/.config"
fi

say
say "=== ConfigFS and USB gadget core ==="
check_config_any "configfs" CONFIG_CONFIGFS_FS
check_config_any "usb gadget core" CONFIG_USB_GADGET
check_config_any "libcomposite" CONFIG_USB_LIBCOMPOSITE
check_config_any "usb gadget configfs" CONFIG_USB_CONFIGFS
check_config_disabled "rockchip/android configfs uevent path" CONFIG_USB_CONFIGFS_UEVENT

check_module_or_builtin libcomposite "libcomposite"
check_path /sys/kernel/config "configfs mount point"
if mountpoint -q /sys/kernel/config; then
  ok "configfs is mounted"
else
  warn "configfs is not mounted; setup script will try: mount -t configfs none /sys/kernel/config"
fi

say
say "=== USB gadget functions ==="
check_config_any "hid gadget function" CONFIG_USB_F_HID CONFIG_USB_CONFIGFS_F_HID
check_config_any "printer gadget function" CONFIG_USB_F_PRINTER CONFIG_USB_CONFIGFS_F_PRINTER
check_config_any "mass storage gadget function" CONFIG_USB_F_MASS_STORAGE CONFIG_USB_CONFIGFS_MASS_STORAGE
check_module_or_builtin usb_f_hid "usb_f_hid"
check_module_or_builtin usb_f_printer "usb_f_printer"
check_module_or_builtin usb_f_mass_storage "usb_f_mass_storage"

say
say "=== UDC / peripheral mode ==="
check_config_any "dwc3 controller" CONFIG_USB_DWC3
check_config_any "dwc3 peripheral or dual-role mode" CONFIG_USB_DWC3_GADGET CONFIG_USB_DWC3_DUAL_ROLE
check_config_any "rockchip usb phy/glue" CONFIG_USB_DWC3_OF_SIMPLE CONFIG_PHY_ROCKCHIP_INNO_USB2 CONFIG_PHY_ROCKCHIP_NANENG_COMBO_PHY CONFIG_USB_ROLE_SWITCH
check_path /sys/class/udc "UDC class"
if [ -d /sys/class/udc ]; then
  udcs="$(ls /sys/class/udc 2>/dev/null | tr '\n' ' ')"
  if [ -n "$udcs" ]; then
    ok "UDC available: $udcs"
  else
    fail "no UDC under /sys/class/udc; check RK3588 OTG device-tree dr_mode/PHY/role-switch"
  fi
  check_path /sys/class/udc/fc000000.usb "C0 UDC for HID/printer"
  check_path /sys/class/udc/fc400000.usb "C1 UDC for MSC"
fi

say
say "=== MSC image local mount support ==="
check_config_any "loop block device" CONFIG_BLK_DEV_LOOP
check_config_any "fat filesystem" CONFIG_FAT_FS
check_config_any "vfat filesystem" CONFIG_VFAT_FS
check_config_any "utf8 nls" CONFIG_NLS_UTF8
check_config_any "fat codepage 437" CONFIG_NLS_CODEPAGE_437
command -v mount >/dev/null 2>&1 && ok "mount command available" || fail "mount command missing"
command -v umount >/dev/null 2>&1 && ok "umount command available" || fail "umount command missing"
if command -v mkfs.vfat >/dev/null 2>&1 || command -v mkfs.fat >/dev/null 2>&1; then
  ok "mkfs.vfat/mkfs.fat available"
else
  fail "mkfs.vfat missing; install dosfstools"
fi

say
say "=== Scanner input support ==="
check_config_any "input core" CONFIG_INPUT
check_config_any "evdev input nodes" CONFIG_INPUT_EVDEV
check_config_any "hid core" CONFIG_HID
check_config_any "usb hid" CONFIG_USB_HID
check_config_any "generic hid" CONFIG_HID_GENERIC
check_path /dev/input "input device directory"
if [ -d /dev/input/by-id ]; then
  ok "/dev/input/by-id exists"
  ls -l /dev/input/by-id 2>/dev/null | sed 's/^/       /'
else
  warn "/dev/input/by-id missing; scanner path may need config change"
fi

say
say "=== Physical printer USB host support ==="
check_config_any "usb host controller" CONFIG_USB_XHCI_HCD CONFIG_USB_XHCI_PLATFORM CONFIG_USB_EHCI_HCD CONFIG_USB_OHCI_HCD
check_config_any "usb printer class" CONFIG_USB_PRINTER
command -v lpstat >/dev/null 2>&1 && ok "CUPS lpstat available" || warn "lpstat missing; install cups"
command -v lpinfo >/dev/null 2>&1 && ok "CUPS lpinfo available" || warn "lpinfo missing; install cups"

say
say "=== GPIO button support ==="
check_config_any "gpiolib" CONFIG_GPIOLIB
check_config_any "gpio userspace api" CONFIG_GPIO_SYSFS CONFIG_GPIO_CDEV
if ls /dev/gpiochip* >/dev/null 2>&1; then
  ok "gpiochip devices present"
  ls -l /dev/gpiochip* 2>/dev/null | sed 's/^/       /'
else
  warn "no /dev/gpiochip*; button config may need sysfs GPIO or kernel GPIO cdev support"
fi

say
say "=== Summary ==="
say "OK=$PASS WARN=$WARN FAIL=$FAIL"
if [ "$FAIL" -gt 0 ]; then
  say "Result: NOT READY. Fix FAIL items before starting rk3588-gateway."
  exit 1
fi

say "Result: kernel surface looks usable. WARN items still need board-level confirmation."
exit 0
