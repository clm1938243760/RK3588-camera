#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import xml.etree.ElementTree as ET
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Optional


DEFAULT_OUTPUT_ROOT = "/var/lib/rk3588-gateway/android_site_probe"
DEFAULT_HDMI_DEVICE = "/dev/video40"
DEFAULT_ADB_ASCII_TEXT = "test123"
DEFAULT_ADB_CLIPBOARD_TEXT = "\u674e\u7fd4"
DEFAULT_HID_TEXT = "123456\n"
ANDROID_COMMON_STORAGE_PATHS = [
    "/sdcard",
    "/sdcard/Download",
    "/sdcard/Documents",
    "/sdcard/Pictures",
    "/sdcard/Android/data",
    "/storage/emulated/0",
    "/mnt/media_rw",
]
ANDROID_REPORT_KEYWORDS = [
    "report",
    "pdf",
    "print",
    "export",
    "\u62a5\u544a",
    "\u6253\u5370",
    "\u5bfc\u51fa",
    "\u4fdd\u5b58",
]
ANDROID_VENDOR_HINTS = {
    "18d1": "Google/Android ADB",
    "04e8": "Samsung Android",
    "12d1": "Huawei/HiSilicon Android",
    "2a45": "Meizu Android",
    "22b8": "Motorola Android",
    "0bb4": "HTC Android",
    "2717": "Xiaomi Android",
    "2d95": "OPPO/OnePlus Android",
    "2a70": "OnePlus Android",
    "0e8d": "MediaTek Android",
    "05c6": "Qualcomm Android",
    "2207": "Rockchip device",
}

ANDROID_PROP_KEYS = [
    "ro.build.version.release",
    "ro.build.version.incremental",
    "ro.build.version.sdk",
    "ro.product.manufacturer",
    "ro.product.brand",
    "ro.product.model",
    "ro.product.name",
    "ro.product.device",
    "ro.product.board",
    "ro.hardware",
    "ro.boot.hardware",
    "ro.serialno",
    "ro.build.display.id",
    "ro.build.fingerprint",
    "ro.product.cpu.abi",
    "persist.sys.locale",
    "ro.sf.lcd_density",
    "sys.usb.config",
    "sys.usb.state",
    "persist.service.adb.enable",
    "service.adb.tcp.port",
]

FIELD_PLAN = """Android all-in-one field plan
=================================

Goal: decide which no-client route is possible.

Route A - USB ADB
  Cable: Android debug/device/OTG port -> RK3588 USB host port.
  Android: enable Developer options + USB debugging, then allow RSA prompt.
  Command:
    sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode usb-adb --label usb_adb
  Optional input test after focusing a safe empty field:
    sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode usb-adb --adb-input-test --adb-clipboard-text 李翔 --label adb_input

Route B - Network ADB
  Cable/network: RK3588 and Android are on the same LAN.
  Android: wireless/network ADB is enabled by the hospital/vendor.
  Command:
    sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode net-adb --adb-target 192.0.2.20:5555 --label net_adb

Route C - HDMI visual route
  Cable: Android HDMI OUT -> RK3588 HDMI RX.
  Command:
    sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode hdmi --label hdmi

Route D - Virtual HID route
  Cable: RK3588 USB device/gadget port -> Android USB host/OTG port.
  Command:
    sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode hid --label hid
  Optional typing test after focusing a safe empty field:
    sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode hid --hid-type-test --hid-text 123456 --label hid_type

Route E - Guided full survey
  Command:
    sudo python3 /opt/rk3588_gateway/scripts/android_site_probe.py --mode guided --label hospital
"""

KEY: dict[str, tuple[int, int]] = {
    "\n": (0, 0x28),
    "\t": (0, 0x2B),
    " ": (0, 0x2C),
    "-": (0, 0x2D),
    "=": (0, 0x2E),
    "[": (0, 0x2F),
    "]": (0, 0x30),
    "\\": (0, 0x31),
    ";": (0, 0x33),
    "'": (0, 0x34),
    "`": (0, 0x35),
    ",": (0, 0x36),
    ".": (0, 0x37),
    "/": (0, 0x38),
    "!": (0x02, 0x1E),
    "@": (0x02, 0x1F),
    "#": (0x02, 0x20),
    "$": (0x02, 0x21),
    "%": (0x02, 0x22),
    "^": (0x02, 0x23),
    "&": (0x02, 0x24),
    "*": (0x02, 0x25),
    "(": (0x02, 0x26),
    ")": (0x02, 0x27),
    "_": (0x02, 0x2D),
    "+": (0x02, 0x2E),
    "{": (0x02, 0x2F),
    "}": (0x02, 0x30),
    "|": (0x02, 0x31),
    ":": (0x02, 0x33),
    '"': (0x02, 0x34),
    "~": (0x02, 0x35),
    "<": (0x02, 0x36),
    ">": (0x02, 0x37),
    "?": (0x02, 0x38),
}
for i in range(10):
    KEY[str(i)] = (0, 0x27 if i == 0 else 0x1D + i)
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEY[ch] = (0, 0x04 + i)
    KEY[ch.upper()] = (0x02, 0x04 + i)


@dataclass
class CmdResult:
    name: str
    command: str
    returncode: Optional[int]
    timed_out: bool
    duration_ms: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0 and not self.timed_out


def safe_name(value: str) -> str:
    text = re.sub(r"[^A-Za-z0-9_.-]+", "_", value.strip())
    return text.strip("._") or "unknown"


def now_stamp() -> str:
    return time.strftime("%Y%m%d_%H%M%S")


def decode_text(data: bytes) -> str:
    return data.decode("utf-8", "replace")


class Probe:
    def __init__(self, args: argparse.Namespace) -> None:
        self.args = args
        label = safe_name(args.label) if args.label else "probe"
        self.outdir = Path(args.output_root) / f"{now_stamp()}_{label}"
        self.outdir.mkdir(parents=True, exist_ok=True)
        self.commands: list[CmdResult] = []
        self.data: dict[str, Any] = {
            "tool": "android_site_probe",
            "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
            "hostname": socket.gethostname(),
            "mode": args.mode,
            "outdir": str(self.outdir),
            "summary": [],
            "files": [],
            "board": {},
            "usb": {},
            "adb": {},
            "hdmi": {},
            "hid": {},
            "routes": {},
            "findings": [],
            "recommendations": [],
        }
        self.lines: list[str] = []

    def add_summary(self, text: str) -> None:
        self.data["summary"].append(text)
        self.lines.append(f"* {text}")
        print(text)

    def add_finding(self, route: str, status: str, text: str) -> None:
        item = {"route": route, "status": status, "text": text}
        self.data["findings"].append(item)
        print(f"[{route}][{status}] {text}")

    def add_file(self, path: Path, note: str) -> None:
        self.data["files"].append({"path": str(path), "note": note, "size": path.stat().st_size if path.exists() else 0})

    def write_text_file(self, name: str, text: str, note: str) -> Path:
        path = self.outdir / name
        path.write_text(text, encoding="utf-8", errors="replace")
        self.add_file(path, note)
        return path

    def run(
        self,
        name: str,
        command: list[str] | str,
        timeout: int = 10,
        shell: bool = False,
        cwd: Optional[str] = None,
    ) -> CmdResult:
        display = command if isinstance(command, str) else " ".join(command)
        start = time.monotonic()
        try:
            proc = subprocess.run(
                command,
                shell=shell,
                cwd=cwd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                timeout=timeout,
                check=False,
            )
            result = CmdResult(
                name=name,
                command=display,
                returncode=proc.returncode,
                timed_out=False,
                duration_ms=int((time.monotonic() - start) * 1000),
                stdout=decode_text(proc.stdout),
                stderr=decode_text(proc.stderr),
            )
        except FileNotFoundError as exc:
            result = CmdResult(
                name=name,
                command=display,
                returncode=127,
                timed_out=False,
                duration_ms=int((time.monotonic() - start) * 1000),
                stdout="",
                stderr=str(exc),
            )
        except subprocess.TimeoutExpired as exc:
            result = CmdResult(
                name=name,
                command=display,
                returncode=None,
                timed_out=True,
                duration_ms=int((time.monotonic() - start) * 1000),
                stdout=decode_text(exc.stdout or b""),
                stderr=decode_text(exc.stderr or b"") + f"\nTIMEOUT after {timeout}s",
            )
        self.commands.append(result)
        return result

    def save_command_logs(self) -> None:
        command_dir = self.outdir / "commands"
        command_dir.mkdir(exist_ok=True)
        for idx, result in enumerate(self.commands, start=1):
            base = f"{idx:03d}_{safe_name(result.name)}"
            payload = {
                "name": result.name,
                "command": result.command,
                "returncode": result.returncode,
                "timed_out": result.timed_out,
                "duration_ms": result.duration_ms,
                "stdout_file": f"{base}.stdout.txt",
                "stderr_file": f"{base}.stderr.txt",
            }
            (command_dir / f"{base}.json").write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            (command_dir / f"{base}.stdout.txt").write_text(result.stdout, encoding="utf-8", errors="replace")
            (command_dir / f"{base}.stderr.txt").write_text(result.stderr, encoding="utf-8", errors="replace")
        self.add_file(command_dir, "raw command logs")

    def finish(self) -> None:
        self.make_recommendations()
        self.save_command_logs()
        self.data["commands"] = [asdict(item) for item in self.commands]
        self.data["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
        field_plan = self.outdir / "FIELD_PLAN.txt"
        field_plan.write_text(FIELD_PLAN, encoding="utf-8")
        self.add_file(field_plan, "field cable and command plan")
        report_json = self.outdir / "report.json"
        report_json.write_text(json.dumps(self.data, ensure_ascii=False, indent=2), encoding="utf-8")

        report_txt = self.outdir / "report.txt"
        report_txt.write_text(self.render_report(), encoding="utf-8")
        self.update_latest_link()

        print("")
        print(f"Report directory: {self.outdir}")
        print(f"Text report: {report_txt}")
        print(f"JSON report: {report_json}")

    def update_latest_link(self) -> None:
        if os.name == "nt":
            return
        latest = Path(self.args.output_root) / "latest"
        try:
            if latest.is_symlink() or latest.exists():
                latest.unlink()
            latest.symlink_to(self.outdir)
        except OSError:
            pass

    def render_report(self) -> str:
        route_lines = self.render_route_status()
        parts = [
            "Android All-in-one Site Probe / 安卓一体机现场探测",
            "=" * 58,
            f"Started: {self.data.get('started_at')}",
            f"Finished: {self.data.get('finished_at')}",
            f"Host: {self.data.get('hostname')}",
            f"Mode: {self.args.mode}",
            f"Output: {self.outdir}",
            "",
            "Route Status / 路线判断",
            "-------------------------",
        ]
        parts.extend(route_lines or ["* No route status generated."])
        parts.extend([
            "",
            "Summary",
            "-------",
        ])
        parts.extend(self.lines or ["* No summary generated."])
        parts.extend(["", "Findings / 关键发现", "-------------------"])
        findings = self.data.get("findings", [])
        if findings:
            for item in findings:
                parts.append(f"* [{item['route']}][{item['status']}] {item['text']}")
        else:
            parts.append("* No finding generated.")
        parts.extend(["", "Recommendations", "---------------"])
        recommendations = self.data.get("recommendations", [])
        parts.extend([f"* {item}" for item in recommendations] or ["* No recommendation generated."])
        parts.extend(["", "How To Rerun / 现场命令", "----------------------"])
        parts.extend(FIELD_PLAN.strip().splitlines())
        parts.extend(["", "Important Files", "---------------"])
        for item in self.data.get("files", []):
            parts.append(f"* {item['path']} ({item['note']}, {item.get('size', 0)} bytes)")
        return "\n".join(parts) + "\n"

    def render_route_status(self) -> list[str]:
        routes = self.data.get("routes", {})
        labels = [
            ("usb_adb", "USB ADB"),
            ("network_adb", "Network ADB"),
            ("hdmi_visual", "HDMI visual"),
            ("hid_input", "Virtual HID"),
            ("file_access", "Android file access"),
        ]
        lines: list[str] = []
        for key, label in labels:
            item = routes.get(key) or {}
            status = item.get("status", "unknown")
            reason = item.get("reason", "")
            lines.append(f"* {label}: {status}" + (f" - {reason}" if reason else ""))
        return lines

    def make_recommendations(self) -> None:
        recommendations: list[str] = []
        routes = self.data.get("routes", {})
        adb_devices = self.data.get("adb", {}).get("devices", [])
        ready_adb = [item for item in adb_devices if item.get("state") == "device"]
        if ready_adb:
            recommendations.append("ADB is usable. This is the preferred no-client route for Android screenshots, UI XML, Chinese input, and file access.")
        elif adb_devices:
            recommendations.append("ADB sees a device but it is not authorized/online. Check the Android RSA authorization prompt and USB debugging setting.")
        elif self.mode_needs_adb():
            recommendations.append("ADB did not see a usable Android device. Confirm the cable is plugged into the Android debug/device port rather than a host-only mouse/keyboard port.")

        hdmi = self.data.get("hdmi", {})
        if hdmi.get("capture_ok"):
            recommendations.append("HDMI RX capture works. If ADB is forbidden, use HDMI visual recognition plus HID/mouse control.")
        elif self.mode_needs_hdmi():
            recommendations.append("HDMI capture did not produce an image. Confirm the all-in-one has HDMI OUT and that it is connected to RK3588 HDMI RX.")

        hid = self.data.get("hid", {})
        if hid.get("c0_state") == "configured" and hid.get("keyboard_exists") and hid.get("mouse_exists"):
            recommendations.append("RK3588 HID gadget is enumerated by the host. English/numeric keyboard input and mouse clicks should be possible.")
        elif self.mode_needs_hid():
            recommendations.append("HID gadget is not configured. Confirm the RK3588 device USB port is connected to an Android host/OTG-capable port.")

        if routes.get("file_access", {}).get("status") == "usable":
            recommendations.append("Android read access over ADB is available. Do not run Android storage write tests unless explicitly approved.")
        if not ready_adb and not hdmi.get("capture_ok") and routes.get("hid_input", {}).get("status") != "usable":
            recommendations.append("No complete no-client automation route is proven yet. Ask the hospital/vendor about USB debugging, HDMI OUT, report export folder, or print/PDF output.")
        if not recommendations:
            recommendations.append("Run --mode all with the expected cable connected, then inspect report.txt and captured screenshots.")
        self.data["recommendations"] = recommendations

    def mode_needs_adb(self) -> bool:
        return self.args.mode in ("all", "guided", "usb-adb", "net-adb", "adb")

    def mode_needs_hdmi(self) -> bool:
        return self.args.mode in ("all", "guided", "hdmi")

    def mode_needs_hid(self) -> bool:
        return self.args.mode in ("all", "guided", "hid")


def read_first(paths: list[str]) -> Optional[str]:
    for item in paths:
        path = Path(item)
        try:
            if path.exists():
                return path.read_text(encoding="utf-8", errors="replace").strip()
        except OSError:
            continue
    return None


def collect_usb_sysfs() -> list[dict[str, str]]:
    root = Path("/sys/bus/usb/devices")
    fields = [
        "busnum",
        "devnum",
        "idVendor",
        "idProduct",
        "bDeviceClass",
        "bDeviceSubClass",
        "bDeviceProtocol",
        "manufacturer",
        "product",
        "serial",
        "speed",
        "version",
    ]
    devices: list[dict[str, str]] = []
    if not root.exists():
        return devices
    for dev in sorted(root.iterdir(), key=lambda p: p.name):
        if not (dev / "idVendor").exists():
            continue
        info: dict[str, str] = {"sysfs": str(dev), "name": dev.name}
        for field in fields:
            path = dev / field
            if path.exists():
                try:
                    info[field] = path.read_text(encoding="utf-8", errors="replace").strip()
                except OSError:
                    pass
        vendor = info.get("idVendor", "").lower()
        product_text = " ".join([info.get("manufacturer", ""), info.get("product", "")]).lower()
        hints: list[str] = []
        if vendor in ANDROID_VENDOR_HINTS:
            hints.append(ANDROID_VENDOR_HINTS[vendor])
        if "android" in product_text or "adb" in product_text:
            hints.append("Android/ADB text hint")
        if hints:
            info["hint"] = "; ".join(dict.fromkeys(hints))
        devices.append(info)
    return devices


def usb_devices_table(devices: list[dict[str, str]]) -> str:
    lines = [
        "name bus dev vendor product speed manufacturer product_text serial hint",
        "---- --- --- ------ ------- ----- ------------ ------------ ------ ----",
    ]
    for item in devices:
        lines.append(
            " ".join(
                [
                    item.get("name", "-"),
                    item.get("busnum", "-"),
                    item.get("devnum", "-"),
                    item.get("idVendor", "-"),
                    item.get("idProduct", "-"),
                    item.get("speed", "-"),
                    quote_cell(item.get("manufacturer", "-")),
                    quote_cell(item.get("product", "-")),
                    quote_cell(item.get("serial", "-")),
                    quote_cell(item.get("hint", "-")),
                ]
            )
        )
    return "\n".join(lines) + "\n"


def quote_cell(value: str) -> str:
    text = value.strip() or "-"
    if " " in text:
        return '"' + text.replace('"', "'") + '"'
    return text


def probe_board(probe: Probe) -> None:
    probe.add_summary("Collecting RK3588 board, USB, and network baseline.")
    commands = [
        ("uname", ["uname", "-a"]),
        ("date", ["date", "-Iseconds"]),
        ("hostnamectl", ["hostnamectl"]),
        ("ip_br_addr", ["ip", "-br", "addr"]),
        ("ip_route", ["ip", "route"]),
        ("ip_neigh", ["ip", "neigh"]),
        ("lsusb", ["lsusb"]),
        ("lsusb_tree", ["lsusb", "-t"]),
        ("usb_devices", "usb-devices 2>/dev/null || true"),
        ("udc_list", "ls /sys/class/udc 2>/dev/null || true"),
        ("gadget_udc", "find /sys/kernel/config/usb_gadget -maxdepth 3 -name UDC -print -exec cat {} \\; 2>/dev/null || true"),
        ("gadget_details", "find /sys/kernel/config/usb_gadget -maxdepth 4 -type f \\( -name UDC -o -name idVendor -o -name idProduct -o -name product -o -name configuration -o -name report_length -o -name q_len -o -name file \\) -print -exec cat {} \\; 2>/dev/null || true"),
        ("hid_printer_nodes", "ls -l /dev/hidg0 /dev/hidg1 /dev/g_printer0 2>/dev/null || true"),
        ("dmesg_usb_tail", "dmesg | grep -Ei 'usb|adb|android|dwc3|hid|mass storage|printer' | tail -160"),
    ]
    for name, cmd in commands:
        result = probe.run(name, cmd, timeout=12, shell=isinstance(cmd, str))
        if name == "lsusb":
            probe.data["usb"]["lsusb"] = result.stdout.strip()
    probe.data["board"]["uname"] = next((c.stdout.strip() for c in probe.commands if c.name == "uname"), "")
    probe.data["board"]["hostname"] = socket.gethostname()
    probe.data["usb"]["sysfs_devices"] = collect_usb_sysfs()
    probe.write_text_file("usb_sysfs_devices.txt", usb_devices_table(probe.data["usb"]["sysfs_devices"]), "USB sysfs device table")

    usb_count = len(probe.data["usb"].get("sysfs_devices", []))
    android_hints = [
        item
        for item in probe.data["usb"].get("sysfs_devices", [])
        if item.get("hint")
    ]
    probe.add_summary(f"USB sysfs devices found: {usb_count}.")
    if android_hints:
        hint_text = "; ".join(
            f"{item.get('idVendor')}:{item.get('idProduct')} {item.get('product', '')} [{item.get('hint')}]"
            for item in android_hints
        )
        probe.add_finding("USB", "hint", f"Possible Android-related USB device(s): {hint_text}")


def parse_adb_devices(output: str) -> list[dict[str, str]]:
    devices: list[dict[str, str]] = []
    for raw in output.splitlines():
        line = raw.strip()
        if not line or line.startswith("List of devices"):
            continue
        parts = line.split()
        if len(parts) < 2:
            continue
        devices.append({"serial": parts[0], "state": parts[1], "details": " ".join(parts[2:])})
    return devices


def adb_cmd(serial: str, *args: str) -> list[str]:
    return ["adb", "-s", serial, *args]


def adb_shell(serial: str, command: str) -> list[str]:
    return ["adb", "-s", serial, "shell", command]


def adb_pull(probe: Probe, serial: str, remote: str, local: Path, note: str) -> None:
    result = probe.run(f"adb_pull_{safe_name(serial)}_{safe_name(local.name)}", adb_cmd(serial, "pull", remote, str(local)), timeout=30)
    if result.ok and local.exists():
        probe.add_file(local, note)


def file_sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def parse_png_size(path: Path) -> Optional[tuple[int, int]]:
    try:
        data = path.read_bytes()[:24]
    except OSError:
        return None
    if len(data) >= 24 and data[:8] == b"\x89PNG\r\n\x1a\n":
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return width, height
    return None


def parse_jpeg_size(path: Path) -> Optional[tuple[int, int]]:
    try:
        data = path.read_bytes()
    except OSError:
        return None
    if len(data) < 4 or data[0:2] != b"\xff\xd8":
        return None
    idx = 2
    while idx + 9 < len(data):
        if data[idx] != 0xFF:
            idx += 1
            continue
        marker = data[idx + 1]
        idx += 2
        if marker in (0xD8, 0xD9):
            continue
        if idx + 2 > len(data):
            return None
        length = int.from_bytes(data[idx : idx + 2], "big")
        if length < 2 or idx + length > len(data):
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if length >= 7:
                height = int.from_bytes(data[idx + 3 : idx + 5], "big")
                width = int.from_bytes(data[idx + 5 : idx + 7], "big")
                return width, height
            return None
        idx += length
    return None


def summarize_uiautomator_xml(path: Path) -> dict[str, Any]:
    summary: dict[str, Any] = {
        "exists": path.exists(),
        "node_count": 0,
        "clickable_count": 0,
        "editable_count": 0,
        "password_count": 0,
        "sample_texts": [],
        "packages": [],
    }
    if not path.exists():
        return summary
    try:
        root = ET.parse(path).getroot()
    except ET.ParseError as exc:
        summary["error"] = str(exc)
        return summary
    texts: list[str] = []
    packages: list[str] = []
    for node in root.iter("node"):
        summary["node_count"] += 1
        attrs = node.attrib
        if attrs.get("clickable") == "true":
            summary["clickable_count"] += 1
        class_name = attrs.get("class", "")
        if attrs.get("password") == "true":
            summary["password_count"] += 1
        if "EditText" in class_name or attrs.get("editable") == "true":
            summary["editable_count"] += 1
        text = (attrs.get("text") or attrs.get("content-desc") or "").strip()
        if text:
            texts.append(text)
        package = attrs.get("package", "").strip()
        if package:
            packages.append(package)
    summary["sample_texts"] = texts[:30]
    summary["packages"] = sorted(set(packages))[:20]
    return summary


def parse_wm_size(text: str) -> Optional[str]:
    match = re.search(r"Physical size:\s*(\d+x\d+)", text)
    if match:
        return match.group(1)
    match = re.search(r"Override size:\s*(\d+x\d+)", text)
    if match:
        return match.group(1)
    return None


def extract_focus_package(text: str) -> str:
    match = re.search(r"mCurrentFocus=.*?\s([A-Za-z0-9_.]+)/", text)
    if match:
        return match.group(1)
    match = re.search(r"mFocusedApp=.*?\s([A-Za-z0-9_.]+)/", text)
    if match:
        return match.group(1)
    return ""


def build_report_find_command() -> str:
    path_expr = " ".join(ANDROID_COMMON_STORAGE_PATHS)
    name_expr = " -o ".join(f"-iname '*{keyword}*'" for keyword in ANDROID_REPORT_KEYWORDS)
    return (
        "for p in "
        + path_expr
        + "; do [ -d $p ] && find $p -maxdepth 4 \\( "
        + name_expr
        + " \\) -print 2>/dev/null | head -200; done"
    )


def collect_adb_device(probe: Probe, device: dict[str, str]) -> dict[str, Any]:
    serial = device["serial"]
    safe_serial = safe_name(serial)
    info: dict[str, Any] = {"serial": serial, "state": device.get("state"), "details": device.get("details", "")}
    if device.get("state") != "device":
        return info

    prop_values: dict[str, str] = {}
    for key in ANDROID_PROP_KEYS:
        result = probe.run(f"adb_getprop_{safe_serial}_{safe_name(key)}", adb_shell(serial, f"getprop {key}"), timeout=8)
        prop_values[key] = result.stdout.strip()
    info["properties"] = prop_values
    model = prop_values.get("ro.product.model") or serial
    android_version = prop_values.get("ro.build.version.release", "")
    sdk = prop_values.get("ro.build.version.sdk", "")
    probe.add_finding("ADB", "device", f"{serial}: {model}, Android {android_version}, SDK {sdk}")

    for name, cmd in [
        ("adb_state", adb_cmd(serial, "get-state")),
        ("adb_shell_identity", adb_shell(serial, "echo rk3588_adb_shell_ok; id; whoami 2>/dev/null || true")),
        ("adb_wm_size", adb_shell(serial, "wm size")),
        ("adb_wm_density", adb_shell(serial, "wm density")),
        ("adb_settings_adb_enabled", adb_shell(serial, "settings get global adb_enabled")),
        ("adb_usb_state", adb_shell(serial, "getprop sys.usb.config; getprop sys.usb.state; getprop service.adb.tcp.port")),
        ("adb_current_focus", adb_shell(serial, "dumpsys window windows | grep -E 'mCurrentFocus|mFocusedApp' | head -20")),
        ("adb_input_method", adb_shell(serial, "dumpsys input_method | grep -E 'mCurMethod|mCurrentInputMethodSubtype|mServedView|mInputShown' | head -80")),
        ("adb_ime_list", adb_shell(serial, "ime list -a 2>/dev/null | head -160")),
        ("adb_storage_listing", adb_shell(serial, "ls -la /sdcard /sdcard/Download 2>/dev/null | head -120")),
        ("adb_mounts_storage", adb_shell(serial, "mount | grep -Ei 'sdcard|emulated|media_rw|fuse|mtp|usb' | head -120")),
    ]:
        result = probe.run(f"{name}_{safe_serial}", cmd, timeout=12)
        info[name] = {"returncode": result.returncode, "stdout": result.stdout.strip(), "stderr": result.stderr.strip()}

    wm_size = parse_wm_size((info.get("adb_wm_size") or {}).get("stdout", ""))
    if wm_size:
        info["screen_size"] = wm_size
        probe.add_finding("ADB", "display", f"{serial}: Android screen size {wm_size}")
    focus_package = extract_focus_package((info.get("adb_current_focus") or {}).get("stdout", ""))
    if focus_package:
        info["focus_package"] = focus_package
        probe.add_finding("ADB", "focus", f"{serial}: current foreground package appears to be {focus_package}")

    getprop_all = probe.run(f"adb_getprop_all_{safe_serial}", adb_shell(serial, "getprop"), timeout=12)
    probe.write_text_file(f"adb_{safe_serial}_getprop.txt", getprop_all.stdout, "Android getprop output")

    display = probe.run(f"adb_dumpsys_display_{safe_serial}", adb_shell(serial, "dumpsys display | head -220"), timeout=15)
    probe.write_text_file(f"adb_{safe_serial}_dumpsys_display.txt", display.stdout, "Android display dump")

    detail_commands = [
        ("dumpsys_window", "dumpsys window windows 2>/dev/null"),
        ("dumpsys_activity_top", "dumpsys activity top 2>/dev/null"),
        ("dumpsys_input_method", "dumpsys input_method 2>/dev/null"),
        ("dumpsys_usb", "dumpsys usb 2>/dev/null"),
        ("dumpsys_power", "dumpsys power 2>/dev/null | head -240"),
        ("dumpsys_battery", "dumpsys battery 2>/dev/null"),
        ("settings_global", "settings list global 2>/dev/null"),
        ("settings_secure", "settings list secure 2>/dev/null"),
        ("settings_system", "settings list system 2>/dev/null"),
        ("pm_features", "pm list features 2>/dev/null"),
        ("pm_packages_all", "pm list packages -f 2>/dev/null"),
        ("pm_packages_third_party", "pm list packages -3 -f 2>/dev/null"),
        ("storage_dirs", "for p in /sdcard /sdcard/Download /sdcard/Documents /sdcard/Pictures /storage/emulated/0 /mnt/media_rw; do echo '###' $p; ls -la $p 2>&1 | head -80; done"),
        ("report_like_files", build_report_find_command()),
        ("network_state", "ip addr 2>/dev/null; echo '--- route ---'; ip route 2>/dev/null; echo '--- netcfg ---'; netcfg 2>/dev/null || true"),
    ]
    detail_files: dict[str, str] = {}
    for name, command in detail_commands:
        result = probe.run(f"adb_{name}_{safe_serial}", adb_shell(serial, command), timeout=25)
        path = probe.write_text_file(f"adb_{safe_serial}_{name}.txt", result.stdout + result.stderr, f"Android {name}")
        detail_files[name] = str(path)
    info["detail_files"] = detail_files

    remote_screen = "/sdcard/rk3588_probe_screen.png"
    screen_local = probe.outdir / f"adb_{safe_serial}_screencap.png"
    screen = probe.run(f"adb_screencap_{safe_serial}", adb_shell(serial, f"screencap -p {remote_screen}"), timeout=20)
    info["screencap"] = {"returncode": screen.returncode, "stderr": screen.stderr.strip()}
    adb_pull(probe, serial, remote_screen, screen_local, "ADB screenshot")
    if screen_local.exists():
        info["screencap"].update(
            {
                "path": str(screen_local),
                "size": screen_local.stat().st_size,
                "sha256": file_sha256(screen_local),
                "png_size": parse_png_size(screen_local),
            }
        )
        if info["screencap"].get("png_size"):
            probe.add_finding("ADB", "screenshot", f"{serial}: screenshot size {info['screencap']['png_size']}")
    probe.run(f"adb_rm_screencap_{safe_serial}", adb_shell(serial, f"rm -f {remote_screen}"), timeout=8)

    remote_xml = "/sdcard/rk3588_probe_window.xml"
    xml_local = probe.outdir / f"adb_{safe_serial}_window.xml"
    xml = probe.run(f"adb_uiautomator_dump_{safe_serial}", adb_shell(serial, f"uiautomator dump {remote_xml}"), timeout=25)
    info["uiautomator_dump"] = {"returncode": xml.returncode, "stdout": xml.stdout.strip(), "stderr": xml.stderr.strip()}
    adb_pull(probe, serial, remote_xml, xml_local, "ADB uiautomator window XML")
    xml_summary = summarize_uiautomator_xml(xml_local)
    info["uiautomator_dump"]["summary"] = xml_summary
    if xml_summary.get("exists") and not xml_summary.get("error"):
        probe.add_finding(
            "ADB",
            "ui",
            f"{serial}: UI nodes={xml_summary['node_count']} clickable={xml_summary['clickable_count']} editable={xml_summary['editable_count']}",
        )
    probe.run(f"adb_rm_uiautomator_{safe_serial}", adb_shell(serial, f"rm -f {remote_xml}"), timeout=8)

    if probe.args.adb_file_test and not probe.args.allow_adb_file_write:
        info["file_write_test"] = {
            "skipped": True,
            "reason": "--adb-file-test was requested without --allow-adb-file-write",
        }
        probe.add_finding(
            "ADB",
            "file-skipped",
            f"{serial}: skipped Android storage write; add --allow-adb-file-write only when file testing is explicitly approved",
        )
    elif probe.args.adb_file_test:
        remote_test = "/sdcard/Download/rk3588_probe_write_test.txt"
        local_push = probe.outdir / f"adb_{safe_serial}_push_test.txt"
        local_pull = probe.outdir / f"adb_{safe_serial}_pull_test.txt"
        local_push.write_text(f"rk3588_probe_push_{int(time.time())}\n", encoding="utf-8")
        probe.add_file(local_push, "ADB push test local source")
        push = probe.run(f"adb_file_push_test_{safe_serial}", adb_cmd(serial, "push", str(local_push), remote_test), timeout=20)
        write = probe.run(
            f"adb_file_write_test_{safe_serial}",
            adb_shell(serial, f"printf rk3588_probe_{int(time.time())} > {remote_test} && ls -l {remote_test}"),
            timeout=12,
        )
        pull = probe.run(f"adb_file_pull_test_{safe_serial}", adb_cmd(serial, "pull", remote_test, str(local_pull)), timeout=20)
        if local_pull.exists():
            probe.add_file(local_pull, "ADB pull test returned file")
        info["file_write_test"] = {
            "push_returncode": push.returncode,
            "write_returncode": write.returncode,
            "pull_returncode": pull.returncode,
            "stdout": write.stdout.strip(),
            "stderr": write.stderr.strip(),
            "pull_exists": local_pull.exists(),
        }
        if push.ok and write.ok and pull.ok and local_pull.exists():
            probe.add_finding("ADB", "file", f"{serial}: /sdcard/Download write/pull test succeeded")
        else:
            probe.add_finding("ADB", "file", f"{serial}: /sdcard/Download write/pull test failed or incomplete")

    if probe.args.adb_input_test:
        ascii_text = probe.args.adb_ascii_text
        clip_text = probe.args.adb_clipboard_text
        probe.add_summary(f"ADB input test enabled for {serial}; make sure a safe text field is focused.")
        ascii_result = probe.run(f"adb_input_text_{safe_serial}", adb_shell(serial, f"input text {quote_android_input(ascii_text)}"), timeout=10)
        service_args = [
            "adb",
            "-s",
            serial,
            "shell",
            "service",
            "call",
            "clipboard",
            "1",
            "i32",
            "1",
            "i32",
            "1",
            "s16",
            "rk3588",
            "i32",
            "1",
            "s16",
            "text/plain",
            "i32",
            "0",
            "i32",
            "0",
            "i32",
            "1",
            "i32",
            "1",
            "s16",
            clip_text,
            "s16",
            clip_text,
            "i32",
            "0",
            "i32",
            "0",
            "s16",
            "com.android.shell",
        ]
        clip_result = probe.run(f"adb_clipboard_set_{safe_serial}", service_args, timeout=12)
        paste_result = probe.run(f"adb_clipboard_paste_{safe_serial}", adb_shell(serial, "input keyevent 279"), timeout=10)
        info["input_test"] = {
            "ascii_text": ascii_text,
            "clipboard_text": clip_text,
            "ascii_returncode": ascii_result.returncode,
            "clipboard_returncode": clip_result.returncode,
            "paste_returncode": paste_result.returncode,
        }

    return info


def quote_android_input(text: str) -> str:
    # Android input text treats spaces specially. Keep this for ASCII smoke text only.
    return text.replace("%", "%25").replace(" ", "%s")


def probe_adb(probe: Probe) -> None:
    adb_path = shutil.which("adb")
    probe.data["adb"]["adb_path"] = adb_path
    if not adb_path:
        probe.add_summary("adb not found on RK3588. Install android-tools-adb before ADB tests.")
        return

    probe.add_summary(f"adb found: {adb_path}")
    probe.run("adb_version", ["adb", "version"], timeout=8)
    probe.run("adb_start_server", ["adb", "start-server"], timeout=10)

    for target in probe.args.adb_target or []:
        probe.add_summary(f"Trying network ADB connect: {target}")
        probe.run(f"adb_connect_{safe_name(target)}", ["adb", "connect", target], timeout=15)

    devices_result = probe.run("adb_devices", ["adb", "devices", "-l"], timeout=10)
    devices = parse_adb_devices(devices_result.stdout)
    if probe.args.adb_serial:
        devices = [item for item in devices if item.get("serial") == probe.args.adb_serial]
    probe.data["adb"]["devices"] = devices
    probe.add_summary(f"ADB devices listed: {len(devices)}.")

    collected: list[dict[str, Any]] = []
    for device in devices:
        collected.append(collect_adb_device(probe, device))
    probe.data["adb"]["collected_devices"] = collected

    usable = [item for item in devices if item.get("state") == "device"]
    usb_ready = [item for item in usable if ":" not in item.get("serial", "")]
    network_ready = [item for item in usable if ":" in item.get("serial", "")]
    if usb_ready:
        probe.data["routes"]["usb_adb"] = {
            "status": "usable",
            "reason": ", ".join(item["serial"] for item in usb_ready),
        }
    elif devices:
        probe.data["routes"]["usb_adb"] = {
            "status": "blocked",
            "reason": "ADB device listed but not authorized/online" if not usable else "only network ADB device was ready",
        }
    elif probe.args.mode in ("all", "adb", "usb-adb", "guided"):
        probe.data["routes"]["usb_adb"] = {"status": "not_found", "reason": "no USB ADB serial in adb devices"}

    if network_ready:
        probe.data["routes"]["network_adb"] = {
            "status": "usable",
            "reason": ", ".join(item["serial"] for item in network_ready),
        }
    elif probe.args.adb_target:
        probe.data["routes"]["network_adb"] = {"status": "blocked", "reason": "adb connect did not produce an online network device"}
    elif probe.args.mode in ("net-adb",):
        probe.data["routes"]["network_adb"] = {"status": "unknown", "reason": "no --adb-target provided"}

    if usable:
        probe.data["routes"]["file_access"] = {
            "status": "usable",
            "reason": "ADB screenshot/UI pull succeeded; Android storage write test is disabled unless explicitly approved",
        }

    if usable:
        models = []
        for item in collected:
            props = item.get("properties") or {}
            model = props.get("ro.product.model") or item.get("serial")
            version = props.get("ro.build.version.release", "")
            models.append(f"{model} Android {version}".strip())
        probe.add_summary("ADB usable Android device(s): " + "; ".join(models))
    elif devices:
        states = ", ".join(f"{item.get('serial')}={item.get('state')}" for item in devices)
        probe.add_summary(f"ADB saw device(s), but none are ready: {states}")
    else:
        probe.add_summary("ADB saw no Android device.")


def probe_hdmi(probe: Probe) -> None:
    device = probe.args.hdmi_device
    probe.data["hdmi"]["device"] = device
    probe.data["hdmi"]["device_exists"] = Path(device).exists()
    if not Path(device).exists():
        probe.add_summary(f"HDMI RX device not found: {device}")
        return

    probe.add_summary(f"HDMI RX device exists: {device}")
    if shutil.which("v4l2-ctl"):
        for name, args in [
            ("v4l2_hdmi_all", ["v4l2-ctl", f"--device={device}", "--all"]),
            ("v4l2_hdmi_formats", ["v4l2-ctl", f"--device={device}", "--list-formats-ext"]),
            ("v4l2_hdmi_fmt", ["v4l2-ctl", f"--device={device}", "--get-fmt-video"]),
            ("v4l2_hdmi_dv_timings", ["v4l2-ctl", f"--device={device}", "--query-dv-timings"]),
            ("v4l2_hdmi_log_status", ["v4l2-ctl", f"--device={device}", "--log-status"]),
        ]:
            result = probe.run(name, args, timeout=12)
            probe.write_text_file(f"{name}.txt", result.stdout + result.stderr, f"HDMI RX {name}")
    else:
        probe.add_summary("v4l2-ctl not found; skipping detailed HDMI device query.")

    gst = shutil.which("gst-launch-1.0")
    probe.data["hdmi"]["gst_launch"] = gst
    if not gst:
        probe.add_summary("gst-launch-1.0 not found; cannot capture HDMI image.")
        return

    captures: list[dict[str, Any]] = []
    for idx in range(max(1, int(probe.args.hdmi_frames))):
        image_path = probe.outdir / f"hdmi_{idx + 1:02d}.jpg"
        cmd = (
            f"gst-launch-1.0 -q -e v4l2src device={device} num-buffers=1 ! "
            "'video/x-raw,format=BGR,width=1920,height=1080,framerate=60/1' ! "
            f"videoconvert ! jpegenc quality=90 ! filesink location='{image_path}'"
        )
        result = probe.run(f"hdmi_capture_{idx + 1}", cmd, timeout=probe.args.hdmi_timeout, shell=True)
        capture = {
            "path": str(image_path),
            "returncode": result.returncode,
            "ok": result.ok,
            "size": image_path.stat().st_size if image_path.exists() else 0,
            "jpeg_size": parse_jpeg_size(image_path) if image_path.exists() else None,
        }
        captures.append(capture)
        if image_path.exists():
            probe.add_file(image_path, f"HDMI RX screenshot frame {idx + 1}")
        time.sleep(0.2)
    capture_ok = any(item.get("ok") and item.get("size", 0) > 4096 for item in captures)
    probe.data["hdmi"]["capture_ok"] = capture_ok
    probe.data["hdmi"]["captures"] = captures
    probe.data["hdmi"]["capture_path"] = captures[-1]["path"] if captures else ""
    probe.data["hdmi"]["capture_size"] = captures[-1]["size"] if captures else 0
    if capture_ok:
        best = max(captures, key=lambda item: int(item.get("size", 0)))
        probe.data["routes"]["hdmi_visual"] = {"status": "usable", "reason": f"captured {best.get('jpeg_size') or 'image'} size={best.get('size')} bytes"}
        probe.add_summary(f"HDMI capture OK: {best.get('path')} ({best.get('size')} bytes).")
        probe.add_finding("HDMI", "capture", f"best frame {best.get('path')} jpeg_size={best.get('jpeg_size')} bytes={best.get('size')}")
    else:
        probe.data["routes"]["hdmi_visual"] = {"status": "blocked", "reason": "no usable HDMI JPEG frame captured"}
        probe.add_summary("HDMI capture failed or produced a tiny image.")


def probe_hid(probe: Probe) -> None:
    keyboard = Path(probe.args.hid_keyboard)
    mouse = Path(probe.args.hid_mouse)
    probe.data["hid"]["keyboard"] = str(keyboard)
    probe.data["hid"]["mouse"] = str(mouse)
    probe.data["hid"]["keyboard_exists"] = keyboard.exists()
    probe.data["hid"]["mouse_exists"] = mouse.exists()
    probe.data["hid"]["c0_state"] = read_first(["/sys/class/udc/fc000000.usb/state"])
    probe.data["hid"]["c0_speed"] = read_first(["/sys/class/udc/fc000000.usb/current_speed"])
    probe.data["hid"]["c1_state"] = read_first(["/sys/class/udc/fc400000.usb/state"])
    probe.data["hid"]["c1_speed"] = read_first(["/sys/class/udc/fc400000.usb/current_speed"])
    probe.data["hid"]["gadget_udc"] = read_first(["/sys/kernel/config/usb_gadget/rk3588_c0_hid_printer/UDC"])

    probe.run("hid_nodes", "ls -l /dev/hidg0 /dev/hidg1 /dev/g_printer0 2>/dev/null || true", timeout=8, shell=True)
    gadget = probe.run(
        "hid_gadget_descriptor_summary",
        "find /sys/kernel/config/usb_gadget/rk3588_c0_hid_printer -maxdepth 5 -type f -print -exec cat {} \\; 2>/dev/null || true",
        timeout=12,
        shell=True,
    )
    probe.write_text_file("hid_gadget_configfs.txt", gadget.stdout + gadget.stderr, "RK3588 HID/printer gadget configfs summary")
    probe.add_summary(
        "HID gadget state: "
        f"keyboard={keyboard.exists()} mouse={mouse.exists()} "
        f"c0_state={probe.data['hid'].get('c0_state') or 'unknown'}."
    )
    if probe.data["hid"].get("c0_state") == "configured" and keyboard.exists() and mouse.exists():
        probe.data["routes"]["hid_input"] = {"status": "usable", "reason": "C0 gadget configured and /dev/hidg0,/dev/hidg1 exist"}
        probe.add_finding("HID", "configured", "RK3588 virtual keyboard/mouse is enumerated by the connected host")
    else:
        probe.data["routes"]["hid_input"] = {
            "status": "not_configured",
            "reason": f"c0_state={probe.data['hid'].get('c0_state')} keyboard={keyboard.exists()} mouse={mouse.exists()}",
        }

    if probe.args.hid_type_test:
        probe.add_summary(f"HID type test enabled; sending {probe.args.hid_text!r} to {keyboard}.")
        info = hid_type_ascii(keyboard, probe.args.hid_text)
        probe.data["hid"]["type_test"] = info
        if info.get("ok"):
            probe.add_summary("HID keyboard write completed.")
        else:
            probe.add_summary(f"HID keyboard write failed: {info.get('error')}")

    if probe.args.hid_click:
        x, y = parse_xy(probe.args.hid_click)
        probe.add_summary(f"HID click test enabled; clicking x={x} y={y} through {mouse}.")
        info = hid_click_abs(mouse, x, y, probe.args.screen_width, probe.args.screen_height)
        probe.data["hid"]["click_test"] = info
        if info.get("ok"):
            probe.add_summary("HID mouse click write completed.")
        else:
            probe.add_summary(f"HID mouse click failed: {info.get('error')}")


def write_retry(fd: int, data: bytes, deadline: float) -> None:
    while True:
        try:
            os.write(fd, data)
            return
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise
            time.sleep(0.03)


def hid_type_ascii(device: Path, text: str) -> dict[str, Any]:
    if not device.exists():
        return {"ok": False, "error": f"{device} does not exist"}
    unsupported = [ch for ch in text if ch not in KEY]
    if unsupported:
        return {"ok": False, "error": f"unsupported HID ASCII char(s): {unsupported!r}"}
    fd: Optional[int] = None
    try:
        fd = os.open(str(device), os.O_WRONLY | os.O_NONBLOCK)
        deadline = time.monotonic() + 3.0
        for ch in text:
            mod, code = KEY[ch]
            write_retry(fd, bytes([mod, 0, code, 0, 0, 0, 0, 0]), deadline)
            time.sleep(0.03)
            write_retry(fd, bytes(8), deadline)
            time.sleep(0.03)
        return {"ok": True, "sent": text}
    except OSError as exc:
        return {"ok": False, "error": repr(exc)}
    finally:
        if fd is not None:
            os.close(fd)


def hid_click_abs(device: Path, x: int, y: int, screen_width: int, screen_height: int) -> dict[str, Any]:
    if not device.exists():
        return {"ok": False, "error": f"{device} does not exist"}
    ax = max(0, min(32767, int(x * 32767 / max(screen_width - 1, 1))))
    ay = max(0, min(32767, int(y * 32767 / max(screen_height - 1, 1))))
    release = bytes([0, ax & 0xFF, (ax >> 8) & 0xFF, ay & 0xFF, (ay >> 8) & 0xFF])
    press = bytes([1, ax & 0xFF, (ax >> 8) & 0xFF, ay & 0xFF, (ay >> 8) & 0xFF])
    fd: Optional[int] = None
    try:
        fd = os.open(str(device), os.O_WRONLY | os.O_NONBLOCK)
        deadline = time.monotonic() + 3.0
        write_retry(fd, release, deadline)
        time.sleep(0.08)
        write_retry(fd, press, deadline)
        time.sleep(0.08)
        write_retry(fd, release, deadline)
        return {"ok": True, "x": x, "y": y, "ax": ax, "ay": ay}
    except OSError as exc:
        return {"ok": False, "error": repr(exc)}
    finally:
        if fd is not None:
            os.close(fd)


def parse_xy(value: str) -> tuple[int, int]:
    match = re.match(r"^\s*(\d+)\s*,\s*(\d+)\s*$", value)
    if not match:
        raise argparse.ArgumentTypeError("expected X,Y")
    return int(match.group(1)), int(match.group(2))


def prompt_step(title: str, body: str, no_prompt: bool = False) -> None:
    print("")
    print("=" * 72)
    print(title)
    print("=" * 72)
    print(body.strip())
    print("")
    if no_prompt or not sys.stdin.isatty():
        return
    input("Ready? Press Enter to continue...")


def run_guided_probe(probe: Probe) -> None:
    probe_board(probe)
    prompt_step(
        "Step 1 - USB ADB",
        """
Cable:
  Android debug/device/OTG port -> RK3588 USB host port.

Android side:
  Enable Developer options and USB debugging.
  If the RSA authorization dialog appears, choose Allow.

This step collects Android version, model, display size, foreground package,
input method, UI XML, screenshot, packages, settings, and storage hints.
        """,
        probe.args.no_prompt,
    )
    probe_adb(probe)

    prompt_step(
        "Step 2 - HDMI OUT",
        """
Cable:
  Android all-in-one HDMI OUT -> RK3588 HDMI RX.

This step captures several HDMI frames and stores v4l2 timing/format details.
If a screenshot shows the Android medical UI, the visual route can be used.
        """,
        probe.args.no_prompt,
    )
    probe_hdmi(probe)

    prompt_step(
        "Step 3 - Virtual HID",
        """
Cable:
  RK3588 USB device/gadget port -> Android USB host/OTG port.

This step checks whether Android enumerates RK3588 as keyboard/mouse.
It will not type or click unless --hid-type-test or --hid-click is explicitly added.
        """,
        probe.args.no_prompt,
    )
    probe_hid(probe)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Collect Android all-in-one ADB/HDMI/HID site evidence on RK3588.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--mode", choices=["all", "guided", "board", "adb", "usb-adb", "net-adb", "hdmi", "hid"], default="all")
    parser.add_argument("--output-root", default=DEFAULT_OUTPUT_ROOT)
    parser.add_argument("--label", default="")
    parser.add_argument("--print-plan", action="store_true", help="Print the cable/command field plan and exit.")
    parser.add_argument("--no-prompt", action="store_true", help="Do not wait for Enter in guided mode.")

    parser.add_argument("--adb-target", action="append", default=[], help="Network ADB target, for example 192.0.2.20:5555.")
    parser.add_argument("--adb-serial", default="", help="Only collect this adb serial.")
    parser.add_argument("--adb-input-test", action="store_true", help="Type ASCII and paste clipboard text into the focused Android input field.")
    parser.add_argument("--adb-file-test", action="store_true", help="Request Android storage write test; skipped unless --allow-adb-file-write is also set.")
    parser.add_argument(
        "--allow-adb-file-write",
        action="store_true",
        help="Required together with --adb-file-test before writing a probe file to Android storage.",
    )
    parser.add_argument("--adb-ascii-text", default=DEFAULT_ADB_ASCII_TEXT)
    parser.add_argument("--adb-clipboard-text", default=DEFAULT_ADB_CLIPBOARD_TEXT)

    parser.add_argument("--hdmi-device", default=DEFAULT_HDMI_DEVICE)
    parser.add_argument("--hdmi-timeout", type=int, default=15)
    parser.add_argument("--hdmi-frames", type=int, default=3, help="Number of HDMI screenshots to capture.")

    parser.add_argument("--hid-keyboard", default="/dev/hidg0")
    parser.add_argument("--hid-mouse", default="/dev/hidg1")
    parser.add_argument("--hid-type-test", action="store_true", help="Send --hid-text through the HID keyboard gadget.")
    parser.add_argument("--hid-text", default=DEFAULT_HID_TEXT)
    parser.add_argument("--hid-click", default="", help="Send one HID absolute mouse click, format X,Y.")
    parser.add_argument("--screen-width", type=int, default=1920)
    parser.add_argument("--screen-height", type=int, default=1080)
    return parser


def main() -> int:
    parser = build_parser()
    args = parser.parse_args()
    if args.print_plan:
        print(FIELD_PLAN)
        return 0
    probe = Probe(args)
    try:
        if args.mode == "guided":
            run_guided_probe(probe)
        else:
            probe_board(probe)
            if args.mode in ("all", "adb", "usb-adb", "net-adb"):
                probe_adb(probe)
            if args.mode in ("all", "hdmi"):
                probe_hdmi(probe)
            if args.mode in ("all", "hid"):
                probe_hid(probe)
    finally:
        probe.finish()
    return 0


if __name__ == "__main__":
    sys.exit(main())
