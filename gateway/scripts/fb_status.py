#!/usr/bin/env python3
from __future__ import annotations

import argparse
import json
import os
import socket
import struct
import subprocess
import time
import urllib.request
from pathlib import Path
from typing import Any, Optional

from PIL import Image, ImageChops, ImageDraw, ImageFont

FBIOGET_VSCREENINFO = 0x4600
CANVAS_W = 480
CANVAS_H = 320


def resample_bilinear() -> int:
    return getattr(getattr(Image, "Resampling", Image), "BILINEAR")


def resample_lanczos() -> int:
    return getattr(getattr(Image, "Resampling", Image), "LANCZOS")

TEXT = {
    "wait_scan": "\u5019\u8bca",
    "select_item": "\u60a3\u8005ID\u626b\u7801",
    "inputting": "\u5f55\u5165",
    "upload_done": "\u62a5\u544a\u4e0a\u4f20\u6210\u529f",
    "not_found": "\u626b\u7801\u672a\u627e\u5230\u7533\u8bf7\u5355",
    "connecting": "\u6b63\u5728\u8fde\u63a5",
    "connected": "\u8fde\u63a5\u6210\u529f",
    "connection_failed": "\u8fde\u63a5\u5931\u8d25",
    "printer_error": "\u672c\u5730\u9700\u8981\u6253\u5370\u673a",
    "service_connecting": "\u670d\u52a1\u8fde\u63a5\u4e2d",
    "querying_order": "\u6b63\u5728\u67e5\u8be2\u7533\u8bf7\u5355",
    "input_done": "\u5f55\u5165\u5b8c\u6210",
    "wait_report": "\u7b49\u5f85\u63a5\u6536\u62a5\u544a",
    "order": "\u7533\u8bf7\u5355",
    "no_selectable_item": "\u672a\u67e5\u8be2\u5230\u53ef\u9009\u62e9\u9879\u76ee",
    "unnamed_item": "\u672a\u547d\u540d\u9879\u76ee",
    "select_hint": "UP/DOWN \u9009\u62e9    OK \u786e\u8ba4",
    "self_check_starting": "\u81ea\u68c0\u542f\u52a8\u4e2d",
    "network": "\u7f51\u7edc",
    "service": "\u670d\u52a1",
    "waiting": "\u7b49\u5f85",
    "service_started": "\u670d\u52a1\u542f\u52a8",
    "assets_dir": "\u667a\u80fd\u4f53UI",
    "brand": "\u7279\u68c0\u667a\u80fd\u4f53",
    "scan_prompt": "\u7b49\u5f85\u626b\u7801",
    "scan_subtitle": "\u8bf7\u626b\u63cf\u60a3\u8005\u6761\u7801",
    "auto_input": "\u6b63\u5728\u81ea\u52a8\u5f55\u5165",
    "do_not_touch": "\u8bf7\u52ff\u64cd\u4f5c\u9f20\u6807\u952e\u76d8",
    "upload_done_title": "\u62a5\u544a\u4e0a\u4f20\u5b8c\u6210",
    "ready_scan": "\u53ef\u4ee5\u7ee7\u7eed\u626b\u7801",
    "file_received": "\u6587\u4ef6\u5df2\u63a5\u6536",
    "no_order_title": "\u672a\u627e\u5230\u7533\u8bf7\u5355",
    "no_order_subtitle": "\u8bf7\u6838\u5bf9\u6761\u7801\u540e\u91cd\u8bd5",
    "connecting_title": "\u6b63\u5728\u542f\u52a8",
    "connecting_subtitle": "\u6b63\u5728\u68c0\u67e5\u7f51\u7edc\u4e0e\u8bbe\u5907",
    "connected_title": "\u670d\u52a1\u5df2\u542f\u52a8",
    "connected_subtitle": "\u7cfb\u7edf\u5c31\u7eea",
    "failed_title": "\u542f\u52a8\u5f02\u5e38",
    "failed_subtitle": "\u8bf7\u68c0\u67e5\u8fde\u63a5\u72b6\u6001",
    "querying_title": "\u6b63\u5728\u67e5\u8be2",
    "querying_subtitle": "\u8bf7\u7a0d\u5019",
    "device_type": "\u8bbe\u5907\u9879\u76ee",
    "patient_items": "\u60a3\u8005\u9879\u76ee",
    "current_input": "\u6b63\u5728\u5f55\u5165",
    "other_items": "\u5176\u4ed6\u9879\u76ee",
    "exam_mismatch": "\u9879\u76ee\u4e0d\u7b26",
    "exam_mismatch_title": "\u60a3\u8005\u68c0\u67e5\u9879\u76ee\u4e0e\u8bbe\u5907\u4e0d\u7b26",
    "exam_mismatch_subtitle": "\u672a\u6267\u884c\u81ea\u52a8\u5f55\u5165",
}

ASSET_PATTERNS = {
    "wait_scan": TEXT["wait_scan"],
    "select_item": TEXT["select_item"],
    "inputting": TEXT["inputting"],
    "upload_done": TEXT["upload_done"],
    "not_found": TEXT["not_found"],
    "connecting": TEXT["connecting"],
    "connected": TEXT["connected"],
    "connection_failed": TEXT["connection_failed"],
    "printer_error": TEXT["printer_error"],
}


def read_state(url: str) -> dict[str, Any]:
    with urllib.request.urlopen(url, timeout=2) as response:
        return json.loads(response.read().decode("utf-8"))


def read_camera_status(url: str) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Cache-Control": "no-cache"})
    with urllib.request.urlopen(request, timeout=1.0) as response:
        return json.loads(response.read().decode("utf-8"))


def read_camera_status_file(path: str) -> dict[str, Any]:
    payload = json.loads(Path(path).read_text(encoding="utf-8"))
    if not isinstance(payload, dict):
        raise ValueError("camera status must be a JSON object")
    corners = payload.get("paper_corners")
    paper_detected = bool(payload.get("paper_detected"))
    paper_detected = paper_detected and isinstance(corners, list) and len(corners) == 4
    result = dict(payload)
    result["paper_detected"] = paper_detected
    result["capture_stage"] = _display_text(
        payload.get("capture_stage") or payload.get("state") or "absent"
    )
    return result


def read_camera_status_source(path: str, url: str) -> dict[str, Any]:
    if path:
        try:
            return read_camera_status_file(path)
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if url:
        return read_camera_status(url)
    return {}


def read_state_file(path: str) -> dict[str, Any]:
    return json.loads(Path(path).read_text(encoding="utf-8"))


def _display_text(value: Any) -> str:
    return str(value or "").strip()


def board_ip() -> str:
    """Return the address used by the default route without network traffic."""
    try:
        import fcntl

        route = Path("/proc/net/route").read_text(encoding="ascii", errors="ignore")
        interface = ""
        for line in route.splitlines()[1:]:
            parts = line.split()
            if len(parts) > 3 and parts[1] == "00000000" and int(parts[3], 16) & 0x2:
                interface = parts[0]
                break
        if interface:
            request = struct.pack("256s", interface.encode("ascii"))
            control = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                address = fcntl.ioctl(control.fileno(), 0x8915, request)[20:24]
            finally:
                control.close()
            return socket.inet_ntoa(address)
    except Exception:
        pass
    try:
        addresses = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
        for item in addresses:
            address = item[4][0]
            if not address.startswith("127."):
                return address
    except Exception:
        pass
    return "--"


CAMERA_PROGRESS_TEXT = {
    "tracking": "检测纸张稳定",
    "locked": "检查纸张文字",
    "collecting_frames": "采集清晰画面",
    "collecting_a": "采集清晰画面",
    "field_a_ready": "准备第二次识别",
    "waiting_b": "准备第二次识别",
    "retry_waiting_a": "重新采集清晰画面",
    "collecting_b": "采集清晰画面",
    "burst_rejected": "重新采集清晰画面",
    "reposition_required": "重新采集清晰画面",
    "queued": "全文 OCR 排队中",
    "ocr_primary": "全文 OCR 中",
    "ocr_refining": "低置信度区域复核",
    "completed": "生成结构化字段",
    "verified": "生成结构化字段",
    "verification_rejected": "生成结构化字段",
    "ocr_error": "全文 OCR 中",
}


class CameraStatusMerger:
    """Merge camera progress while latching a final rejection until paper removal."""

    def __init__(self) -> None:
        self.rejection_key = ""
        self.rejection_cleared = False
        self.completed_capture_id = ""
        self.completed_display: dict[str, Any] = {}

    def merge(self, state: dict[str, Any], camera: dict[str, Any]) -> dict[str, Any]:
        if not isinstance(camera, dict):
            return state
        raw_display = state.get("display") if isinstance(state.get("display"), dict) else {}
        raw_screen = str(raw_display.get("screen", "wait_scan"))
        current = visible_display(state, time.time())
        current_screen = str(current.get("screen", "wait_scan"))
        if raw_screen == "entry_completed":
            completed_capture_id = _display_text(raw_display.get("capture_id"))
            if completed_capture_id:
                self.completed_capture_id = completed_capture_id
                self.completed_display = dict(raw_display)
        if current_screen == "paper_reposition":
            self.completed_capture_id = ""
            self.completed_display = {}
            rejection_key = "%s|%s" % (
                _display_text(current.get("capture_id")),
                _display_text(state.get("updated_at")),
            )
            if rejection_key != self.rejection_key:
                self.rejection_key = rejection_key
                self.rejection_cleared = False

        paper_detected = bool(camera.get("paper_detected"))
        stage = _display_text(camera.get("capture_stage") or camera.get("state"))
        camera_capture_id = _display_text(camera.get("capture_id"))
        completed_capture_id = _display_text(raw_display.get("capture_id"))
        completed_capture_matches = (
            bool(self.completed_capture_id)
            and self.completed_capture_id == camera_capture_id
        ) or (raw_screen == "entry_completed" and (
            (
                bool(completed_capture_id)
                and completed_capture_id == camera_capture_id
            )
            or (
                not completed_capture_id
                and stage in {"completed", "verified"}
                and _display_text(camera.get("reason")) == "waiting_for_paper_removal"
            )
        ))
        if not paper_detected:
            had_completed_capture = bool(self.completed_capture_id)
            self.completed_capture_id = ""
            self.completed_display = {}
            if raw_screen == "entry_completed" or had_completed_capture:
                return self._display_state(state, {"screen": "wait_scan"})
            if current_screen == "paper_reposition":
                self.rejection_cleared = True
            if current_screen not in {"paper_reposition", "report_detected", "report_detecting"}:
                return state
            return self._display_state(state, {"screen": "wait_scan"})

        if self.completed_capture_id and self.completed_capture_id != camera_capture_id:
            self.completed_capture_id = ""
            self.completed_display = {}
            completed_capture_matches = False

        if (
            not completed_capture_matches
            and not self.completed_capture_id
            and raw_screen in {"report_upload_success", "report_upload_failed"}
            and current_screen == "wait_scan"
            and stage in {"completed", "verified"}
            and _display_text(camera.get("reason")) == "waiting_for_paper_removal"
        ):
            self.completed_capture_id = camera_capture_id
            self.completed_display = {
                "screen": "entry_completed",
                "capture_id": camera_capture_id,
            }
            completed_capture_matches = bool(camera_capture_id)

        if completed_capture_matches:
            if current_screen in {
                "report_uploading",
                "report_upload_success",
                "report_upload_failed",
            }:
                return state
            return self._display_state(
                state,
                {
                    **self.completed_display,
                    **(raw_display if raw_screen == "entry_completed" else {}),
                    "screen": "entry_completed",
                    "prompt_text": "请移除申请单",
                },
            )

        protected = {
            "inputting",
            "entering",
            "camera_inputting",
            "entry_completed",
            "report_uploading",
            "report_upload_success",
            "report_upload_failed",
        }
        if current_screen in protected:
            return state
        if current_screen == "paper_reposition" and not self.rejection_cleared:
            return state

        return self._display_state(
            state,
            {
                "screen": "report_detecting",
                "camera_stage": stage,
                "progress_text": CAMERA_PROGRESS_TEXT.get(stage, "检测纸张稳定"),
                "capture_id": _display_text(camera.get("capture_id")),
            },
        )

    @staticmethod
    def _display_state(state: dict[str, Any], display: dict[str, Any]) -> dict[str, Any]:
        merged = dict(state)
        merged.pop("leading_display", None)
        merged.pop("display_not_before", None)
        merged.pop("expires_at", None)
        merged["display"] = {
            "patient_name": "",
            "patient_id": "",
            **display,
        }
        return merged


def render_signature(state: dict[str, Any], error: str = "") -> str:
    """Redraw only when the pixels currently meant to be visible have changed."""
    visible = visible_display(state, time.time())
    return json.dumps([visible, error], ensure_ascii=False, sort_keys=True, separators=(",", ":"))


def visible_display(state: dict[str, Any], now: float) -> dict[str, Any]:
    display = state.get("display") if isinstance(state.get("display"), dict) else {}
    leading = state.get("leading_display")
    display_not_before = state.get("display_not_before")
    try:
        if (
            isinstance(leading, dict)
            and display_not_before is not None
            and float(display_not_before) > now
        ):
            return leading
    except (TypeError, ValueError):
        pass
    try:
        expires_at = state.get("expires_at")
        if expires_at is not None and float(expires_at) <= now:
            return {"screen": "wait_scan"}
    except (TypeError, ValueError):
        pass
    return display


def load_font(size: int, bold: bool = False):
    candidates = (
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Bold.ttc" if bold else "",
        "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-microhei.ttc",
        "/usr/share/fonts/truetype/wqy/wqy-zenhei.ttc",
        "C:/Windows/Fonts/msyh.ttc" if not bold else "C:/Windows/Fonts/msyhbd.ttc",
        "C:/Windows/Fonts/simsun.ttc",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf" if bold else "",
        "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    )
    for path in candidates:
        if path and Path(path).exists():
            return ImageFont.truetype(path, size)
    return ImageFont.load_default()


class Framebuffer:
    def __init__(self, path: str, width: int, height: int, bpp: int, rotate: int = 0) -> None:
        self.path = path
        self.width = width
        self.height = height
        self.bpp = bpp
        self.rotate = rotate

    @classmethod
    def open(cls, path: str, width: int, height: int, bpp: int, rotate: int = 0) -> "Framebuffer":
        try:
            import fcntl

            with Path(path).open("rb", buffering=0) as handle:
                data = fcntl.ioctl(handle.fileno(), FBIOGET_VSCREENINFO, bytes(160))
            values = struct.unpack_from("8I", data)
            width = int(values[0]) or width
            height = int(values[1]) or height
            bpp = int(values[6]) or bpp
        except Exception:
            pass
        return cls(path, width, height, bpp, rotate)

    def write(self, image: Image.Image) -> None:
        frame = Image.new("RGB", (self.width, self.height), "black")
        canvas = image.convert("RGB").resize((CANVAS_W, CANVAS_H), resample_bilinear())
        if self.rotate:
            canvas = canvas.rotate(self.rotate, expand=True)
        canvas = canvas.resize((self.width, self.height), resample_bilinear())
        frame.paste(canvas, (0, 0))
        payload = self._rgb565(frame) if self.bpp == 16 else frame.convert("RGBA").tobytes("raw", "BGRA")
        with Path(self.path).open("r+b", buffering=0) as handle:
            handle.seek(0)
            handle.write(payload)

    def _rgb565(self, image: Image.Image) -> bytes:
        out = bytearray()
        for r, g, b in image.convert("RGB").getdata():
            out.extend(struct.pack("<H", ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)))
        return bytes(out)


class SysfsGpio:
    def __init__(self, number: int) -> None:
        self.number = number
        self.base = Path(f"/sys/class/gpio/gpio{number}")
        self._ensure_exported()

    def _ensure_exported(self) -> None:
        if not self.base.exists():
            Path("/sys/class/gpio/export").write_text(str(self.number), encoding="ascii")
            deadline = time.monotonic() + 1.0
            while not self.base.exists() and time.monotonic() < deadline:
                time.sleep(0.02)
        direction = self.base / "direction"
        if direction.exists():
            direction.write_text("out", encoding="ascii")

    def set(self, value: bool) -> None:
        (self.base / "value").write_text("1" if value else "0", encoding="ascii")


class RawSpi:
    SPI_IOC_WR_MODE = 0x40016B01
    SPI_IOC_WR_BITS_PER_WORD = 0x40016B03
    SPI_IOC_WR_MAX_SPEED_HZ = 0x40046B04

    def __init__(self, path: str, speed_hz: int) -> None:
        import fcntl

        self.fd = os.open(path, os.O_RDWR)
        fcntl.ioctl(self.fd, self.SPI_IOC_WR_MODE, struct.pack("B", 0))
        fcntl.ioctl(self.fd, self.SPI_IOC_WR_BITS_PER_WORD, struct.pack("B", 8))
        fcntl.ioctl(self.fd, self.SPI_IOC_WR_MAX_SPEED_HZ, struct.pack("I", speed_hz))

    def write(self, data: bytes) -> None:
        view = memoryview(data)
        while view:
            written = os.write(self.fd, view[:4096])
            view = view[written:]


class Ili9488Display:
    def __init__(
        self,
        spidev: str,
        dc_gpio: int,
        reset_gpio: Optional[int],
        cs_gpio: Optional[int],
        bl_gpio: Optional[int],
        speed_hz: int,
        rotate: int,
        color_order: str,
        pixel_format: int,
        invert: bool,
    ) -> None:
        self.spi = RawSpi(spidev, speed_hz)
        self.dc = SysfsGpio(dc_gpio)
        self.reset = SysfsGpio(reset_gpio) if reset_gpio is not None else None
        self.cs = SysfsGpio(cs_gpio) if cs_gpio is not None else None
        self.bl = SysfsGpio(bl_gpio) if bl_gpio is not None else None
        if self.cs:
            self.cs.set(True)
        self.rotate = rotate
        self.color_order = color_order
        self.pixel_format = pixel_format
        self.invert = invert
        self.width = CANVAS_W
        self.height = CANVAS_H
        self._last_frame: Optional[Image.Image] = None
        self._init_panel()

    def _init_panel(self) -> None:
        if self.reset:
            self.reset.set(True)
            time.sleep(0.05)
            self.reset.set(False)
            time.sleep(0.10)
            self.reset.set(True)
            time.sleep(0.15)
        if self.bl:
            self.bl.set(True)
        self.command(0x01)
        time.sleep(0.12)
        self.command(0x11)
        time.sleep(0.12)
        self.command(0xE0, bytes((0x00, 0x03, 0x09, 0x08, 0x16, 0x0A, 0x3F, 0x78, 0x4C, 0x09, 0x0A, 0x08, 0x16, 0x1A, 0x0F)))
        self.command(0xE1, bytes((0x00, 0x16, 0x19, 0x03, 0x0F, 0x05, 0x32, 0x45, 0x46, 0x04, 0x0E, 0x0D, 0x35, 0x37, 0x0F)))
        self.command(0xC0, bytes((0x17, 0x15)))
        self.command(0xC1, bytes((0x41,)))
        self.command(0xC5, bytes((0x00, 0x12, 0x80)))
        self.command(0xB0, bytes((0x00,)))
        self.command(0xB1, bytes((0xA0,)))
        self.command(0xB4, bytes((0x02,)))
        self.command(0xB6, bytes((0x02, 0x02)))
        self.command(0x36, bytes([self._madctl(self.rotate)]))
        self.command(0x3A, bytes([0x55 if self.pixel_format == 16 else 0x66]))
        self.command(0xE9, bytes((0x00,)))
        self.command(0xF7, bytes((0xA9, 0x51, 0x2C, 0x82)))
        self.command(0x21 if self.invert else 0x20)
        self.command(0x29)
        time.sleep(0.05)

    def _madctl(self, rotate: int) -> int:
        values = {
            0: 0x40,
            90: 0x20,
            180: 0x80,
            270: 0xE0,
        }
        value = values.get(rotate, 0x20)
        if self.color_order == "bgr":
            value |= 0x08
        return value

    def command(self, command: int, data: bytes = b"") -> None:
        if self.cs:
            self.cs.set(False)
        try:
            self.dc.set(False)
            self.spi.write(bytes([command]))
            if data:
                self.dc.set(True)
                self.spi.write(data)
        finally:
            if self.cs:
                self.cs.set(True)

    def write(self, image: Image.Image) -> None:
        frame = image.convert("RGB").resize((self.width, self.height), resample_bilinear())
        previous = getattr(self, "_last_frame", None)
        if previous is None or previous.size != frame.size:
            dirty = (0, 0, self.width, self.height)
        else:
            dirty = ImageChops.difference(previous, frame).getbbox()
            if dirty is None:
                return
        x0, y0, x1, y1 = dirty
        region = frame.crop(dirty)
        self._set_window(x0, y0, x1 - 1, y1 - 1)
        if self.cs:
            self.cs.set(False)
        try:
            self.dc.set(True)
            self.spi.write(self._pixels(region))
        finally:
            if self.cs:
                self.cs.set(True)
        self._last_frame = frame.copy()

    def _pixels(self, image: Image.Image) -> bytes:
        if self.pixel_format == 16:
            out = bytearray()
            for r, g, b in image.getdata():
                if self.color_order == "bgr":
                    r, b = b, r
                value = ((r & 0xF8) << 8) | ((g & 0xFC) << 3) | (b >> 3)
                out.extend(struct.pack(">H", value))
            return bytes(out)
        if self.color_order == "bgr":
            data = bytearray()
            for r, g, b in image.getdata():
                data.extend((b, g, r))
            return bytes(data)
        return image.tobytes()

    def _set_window(self, x0: int, y0: int, x1: int, y1: int) -> None:
        self.command(0x2A, struct.pack(">HH", x0, x1))
        self.command(0x2B, struct.pack(">HH", y0, y1))
        self.command(0x2C)


class AssetRenderer:
    def __init__(self, assets_dir: Path) -> None:
        self.assets_dir = assets_dir
        self.assets = self._load_assets()
        self.font_tiny = load_font(13)
        self.font_small = load_font(16)
        self.font_mid = load_font(20)
        self.font_big = load_font(25, bold=True)
        self.font_ip = load_font(22, bold=True)

    def _load_assets(self) -> dict[str, Image.Image]:
        loaded: dict[str, Image.Image] = {}
        if not self.assets_dir.exists():
            return loaded
        files = [path for path in self.assets_dir.glob("*.png") if not path.name.startswith("_")]
        for key, pattern in ASSET_PATTERNS.items():
            candidates = [path for path in files if pattern in path.name]
            if not candidates:
                continue
            path = self._best_candidate(candidates)
            try:
                loaded[key] = Image.open(path).convert("RGBA")
            except Exception:
                continue
        return loaded

    def _best_candidate(self, candidates: list[Path]) -> Path:
        for scale in ("@2x", "@3x", "@1x"):
            for path in candidates:
                if scale in path.name:
                    return path
        return candidates[0]

    def render_boot(self, status: str, detail: str = "") -> Image.Image:
        return self._medical_page({"screen": "wait_scan"})

    def render(self, state: dict[str, Any], error: str = "") -> Image.Image:
        display = visible_display(state, time.time())
        return self._medical_page(display)

    def _medical_page(self, display: dict[str, Any]) -> Image.Image:
        image = Image.new("RGB", (CANVAS_W, CANVAS_H), (247, 250, 251))
        draw = ImageDraw.Draw(image)
        ink = (35, 67, 77)
        muted = (104, 126, 132)
        accent = (21, 137, 132)
        draw.text((22, 18), "聚垒科技", font=self.font_small, fill=ink)
        screen = str(display.get("screen", "wait_scan"))
        inputting = screen in {"inputting", "entering", "camera_inputting"}
        if screen == "report_uploading":
            title = "报告上传中"
            accent = (42, 125, 164)
        elif screen == "report_upload_success":
            title = "报告上传成功"
            accent = (35, 145, 101)
        elif screen == "report_upload_failed":
            title = "报告上传失败"
            accent = (190, 72, 72)
        elif screen in {"report_detected", "report_detecting"}:
            title = "正在检测申请单"
            accent = (42, 125, 164)
        elif screen == "paper_reposition":
            title = "请移除并重新摆放申请单"
            accent = (190, 125, 44)
        elif screen == "entry_completed":
            title = "录入完成"
            accent = (35, 145, 101)
        else:
            title = "自动录入中" if inputting else "等待申请单"
        title_font = load_font(24 if len(title) > 8 else 31, bold=True)
        bbox = draw.textbbox((0, 0), title, font=title_font)
        draw.text(((CANVAS_W - bbox[2] + bbox[0]) // 2, 119), title, font=title_font, fill=ink)
        if screen in {"report_detected", "report_detecting"}:
            self._center_text(draw, "请保持申请单稳定", 172, self.font_mid, (*muted, 255))
            progress = self._clip(
                draw,
                _display_text(display.get("progress_text")) or "检测纸张稳定",
                self.font_small,
                360,
            )
            self._center_text(draw, progress, 207, self.font_small, (*muted, 255))
        elif screen == "entry_completed":
            prompt = _display_text(display.get("prompt_text")) or "请移除申请单"
            self._center_text(draw, prompt, 177, self.font_mid, (*muted, 255))
        elif inputting:
            patient_name = self._clip(draw, _display_text(display.get("patient_name")), self.font_small, 300)
            patient_id = self._clip(draw, _display_text(display.get("patient_id")), self.font_small, 300)
            if patient_name:
                self._center_text(draw, "姓名：" + patient_name, 174, self.font_small, (*muted, 255))
            if patient_id:
                self._center_text(draw, "患者ID：" + patient_id, 202, self.font_small, (*muted, 255))
        draw.line((22, 67, CANVAS_W - 22, 67), fill=(215, 229, 230), width=1)
        draw.line((22, 263, CANVAS_W - 22, 263), fill=(215, 229, 230), width=1)
        draw.ellipse((CANVAS_W // 2 - 5, 92, CANVAS_W // 2 + 5, 102), fill=accent)
        ip = "设备 IP：" + board_ip()
        ip_bbox = draw.textbbox((0, 0), ip, font=self.font_ip)
        draw.text((CANVAS_W - 18 - (ip_bbox[2] - ip_bbox[0]), 278), ip, font=self.font_ip, fill=ink)
        return image

    def _with_popup(self, image: Image.Image, popup: Any) -> Image.Image:
        if not isinstance(popup, dict):
            return image
        title = str(popup.get("title", "") or TEXT["file_received"])
        message = str(popup.get("message", "") or "")
        rgba = image.convert("RGBA")
        draw = ImageDraw.Draw(rgba, "RGBA")
        draw.rectangle((0, 0, CANVAS_W, CANVAS_H), fill=(0, 0, 0, 76))
        accent = (35, 196, 123)
        draw.rounded_rectangle((42, 100, 438, 222), radius=24, fill=(255, 255, 255, 246), outline=(*accent, 210), width=2)
        draw.rectangle((42, 138, 50, 188), fill=(*accent, 255))
        self._center_text(draw, self._clip(draw, title, self.font_big, 330), 124, self.font_big, (22, 34, 46, 255))
        if message:
            message = self._clip(draw, message, self.font_mid, 330)
        else:
            message = "\u6b63\u5728\u8f6c\u6362\u5e76\u6253\u5370"
        self._center_text(draw, message, 170, self.font_mid, (73, 88, 105, 255))
        return rgba.convert("RGB")

    def _asset_canvas(self, key: str) -> Image.Image:
        image = self._background()
        asset = self.assets.get(key)
        if asset is None:
            return image
        fitted, x, y = self._cover_asset(asset)
        image.alpha_composite(fitted, (x, y))
        return image.convert("RGB")

    def _cover_asset(self, asset: Image.Image) -> tuple[Image.Image, int, int]:
        scale = max(CANVAS_W / asset.width, CANVAS_H / asset.height)
        w = max(1, int(asset.width * scale))
        h = max(1, int(asset.height * scale))
        fitted = asset.resize((w, h), resample_lanczos())
        left = max((w - CANVAS_W) // 2, 0)
        top = max((h - CANVAS_H) // 2, 0)
        cropped = fitted.crop((left, top, left + CANVAS_W, top + CANVAS_H))
        return cropped, 0, 0

    def _background(self) -> Image.Image:
        image = Image.new("RGBA", (CANVAS_W, CANVAS_H), "#edf4fa")
        pixels = image.load()
        for y in range(CANVAS_H):
            for x in range(CANVAS_W):
                r = 236 - int(y * 20 / CANVAS_H)
                g = 245 - int((x + y) * 15 / (CANVAS_W + CANVAS_H))
                b = 250 - int(x * 12 / CANVAS_W)
                pixels[x, y] = (r, g, b, 255)
        draw = ImageDraw.Draw(image, "RGBA")
        draw.polygon([(0, 58), (CANVAS_W, 20), (CANVAS_W, 82), (0, 122)], fill=(223, 235, 246, 180))
        draw.polygon([(0, 260), (CANVAS_W, 218), (CANVAS_W, CANVAS_H), (0, CANVAS_H)], fill=(216, 231, 242, 190))
        draw.rounded_rectangle((0, 0, CANVAS_W - 1, CANVAS_H - 1), radius=0, outline=(197, 213, 226, 255), width=1)
        return image

    def _status_page(self, title: str, subtitle: str, tag: str, accent: tuple[int, int, int]) -> Image.Image:
        image = self._background()
        draw = ImageDraw.Draw(image, "RGBA")
        self._top_bar(draw, accent, tag)
        draw.rounded_rectangle((28, 84, 452, 248), radius=26, fill=(255, 255, 255, 238), outline=(*accent, 150), width=2)
        draw.rectangle((28, 122, 36, 210), fill=(*accent, 255))
        self._center_text(draw, title, 124, load_font(34, bold=True), (22, 34, 46, 255))
        self._center_text(draw, subtitle, 176, self.font_mid, (73, 88, 105, 255))
        draw.rounded_rectangle((138, 270, 342, 298), radius=14, fill=(*accent, 235))
        self._center_text(draw, tag, 274, self.font_small, (255, 255, 255, 255))
        return image.convert("RGB")

    def _top_bar(self, draw: ImageDraw.ImageDraw, accent: tuple[int, int, int], tag: str) -> None:
        draw.rectangle((0, 0, CANVAS_W, 58), fill=(12, 20, 30, 255))
        draw.rectangle((0, 56, CANVAS_W, 60), fill=(*accent, 255))
        draw.text((24, 16), TEXT["brand"], font=self.font_mid, fill=(246, 250, 255, 255))
        badge_w = min(max(int(draw.textlength(tag, font=self.font_tiny)) + 30, 92), 180)
        x0 = CANVAS_W - badge_w - 22
        draw.rounded_rectangle((x0, 15, CANVAS_W - 22, 43), radius=14, fill=(*accent, 235))
        draw.text((x0 + 15, 20), tag[:10], font=self.font_tiny, fill=(255, 255, 255, 255))

    def _select(self, display: dict[str, Any]) -> Image.Image:
        image = self._background()
        draw = ImageDraw.Draw(image, "RGBA")
        items = display.get("items") if isinstance(display.get("items"), list) else []
        selected = int(display.get("selected_index", 0) or 0)
        scan = str(display.get("scan", "") or "")
        accent = (70, 143, 255)
        self._top_bar(draw, accent, TEXT["select_item"])
        draw.rounded_rectangle((24, 78, 456, 292), radius=24, fill=(255, 255, 255, 238), outline=(204, 222, 238, 255), width=1)
        draw.text((48, 96), f"{TEXT['order']} {scan.upper()}", font=self.font_mid, fill=(33, 48, 64, 255))

        y = 138
        visible_items = []
        if items:
            selected = selected % len(items)
            visible_items = [
                items[(selected + offset) % len(items)]
                for offset in range(min(3, len(items)))
            ]
        if not visible_items:
            draw.text((68, 170), TEXT["no_selectable_item"], font=self.font_mid, fill=(86, 104, 122, 255))
        for index, item in enumerate(visible_items):
            if not isinstance(item, dict):
                continue
            active = index == 0
            row_fill = (*accent, 255) if active else (244, 248, 252, 255)
            text_fill = (255, 255, 255, 255) if active else (35, 50, 65, 255)
            outline = (*accent, 255) if active else (218, 228, 238, 255)
            draw.rounded_rectangle((48, y, 432, y + 40), radius=13, fill=row_fill, outline=outline, width=1)
            text = str(item.get("exam_item", "") or item.get("title", "") or TEXT["unnamed_item"])
            draw.text((68, y + 8), self._clip(draw, text, self.font_mid, 340), font=self.font_mid, fill=text_fill)
            y += 48
        draw.text((48, 263), TEXT["select_hint"], font=self.font_small, fill=(91, 109, 127, 255))
        return image.convert("RGB")

    def _inputting(self, display: dict[str, Any]) -> Image.Image:
        exam_item = str(display.get("exam_item", "") or "")
        other_items = display.get("other_exam_items") if isinstance(display.get("other_exam_items"), list) else []
        patient_items = display.get("patient_exam_items") if isinstance(display.get("patient_exam_items"), list) else []
        lines = []
        if exam_item:
            lines.append(f"{TEXT['current_input']}: {exam_item}")
        if other_items:
            lines.append(f"{TEXT['other_items']}: {', '.join(str(item) for item in other_items if item)}")
        elif len(patient_items) > 1:
            lines.append(f"{TEXT['patient_items']}: {', '.join(str(item) for item in patient_items if item)}")
        return self._detail_status_page(TEXT["auto_input"], TEXT["do_not_touch"], TEXT["inputting"], (255, 183, 77), lines)

    def _exam_mismatch(self, display: dict[str, Any]) -> Image.Image:
        device_type = str(display.get("device_type", "") or "")
        patient_items = display.get("patient_exam_items") if isinstance(display.get("patient_exam_items"), list) else []
        lines = []
        if device_type:
            lines.append(f"{TEXT['device_type']}: {device_type}")
        if patient_items:
            lines.append(f"{TEXT['patient_items']}: {', '.join(str(item) for item in patient_items if item)}")
        return self._detail_status_page(
            TEXT["exam_mismatch_title"],
            TEXT["exam_mismatch_subtitle"],
            TEXT["exam_mismatch"],
            (245, 92, 92),
            lines,
        )

    def _detail_status_page(
        self,
        title: str,
        subtitle: str,
        tag: str,
        accent: tuple[int, int, int],
        lines: list[str],
    ) -> Image.Image:
        image = self._background()
        draw = ImageDraw.Draw(image, "RGBA")
        self._top_bar(draw, accent, tag)
        draw.rounded_rectangle((24, 76, 456, 292), radius=22, fill=(255, 255, 255, 238), outline=(*accent, 150), width=2)
        draw.rectangle((24, 114, 32, 258), fill=(*accent, 255))
        self._center_text(draw, self._clip(draw, title, load_font(28, bold=True), 380), 100, load_font(28, bold=True), (22, 34, 46, 255))
        self._center_text(draw, subtitle, 138, self.font_mid, (73, 88, 105, 255))
        y = 178
        for line in lines[:3]:
            clipped = self._clip(draw, str(line), self.font_small, 350)
            draw.text((56, y), clipped, font=self.font_small, fill=(35, 50, 65, 255))
            y += 28
        return image.convert("RGB")

    def _draw_center_overlay(self, image: Image.Image, title: str, subtitle: str) -> None:
        rgba = image.convert("RGBA")
        draw = ImageDraw.Draw(rgba, "RGBA")
        draw.rounded_rectangle((42, 98, 438, 220), radius=24, fill=(255, 255, 255, 238), outline=(204, 222, 238, 255), width=1)
        self._center_text(draw, title, 126, self.font_big, (30, 46, 63, 255))
        self._center_text(draw, subtitle, 166, self.font_mid, (71, 91, 111, 255))
        image.paste(rgba.convert("RGB"))

    def _draw_footer(self, image: Image.Image, text: str) -> None:
        rgba = image.convert("RGBA")
        draw = ImageDraw.Draw(rgba, "RGBA")
        y = CANVAS_H - 44
        draw.rounded_rectangle((70, y, 410, y + 30), radius=13, fill=(255, 255, 255, 220))
        self._center_text(draw, text, y + 5, self.font_small, (61, 79, 97, 255))
        image.paste(rgba.convert("RGB"))

    def _center_text(self, draw: ImageDraw.ImageDraw, text: str, y: int, font: ImageFont.ImageFont, fill: tuple[int, int, int, int]) -> None:
        bbox = draw.textbbox((0, 0), text, font=font)
        x = max((CANVAS_W - (bbox[2] - bbox[0])) // 2, 0)
        draw.text((x, y), text, font=font, fill=fill)

    def _clip(self, draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, max_width: int) -> str:
        if draw.textlength(text, font=font) <= max_width:
            return text
        out = text
        while out and draw.textlength(out + "...", font=font) > max_width:
            out = out[:-1]
        return out + "..." if out else "..."


def hide_console() -> None:
    for tty in ("/dev/tty0", "/dev/tty1"):
        try:
            with Path(tty).open("w", encoding="ascii", errors="ignore") as handle:
                handle.write("\033[2J\033[H\033[?25l")
        except Exception:
            pass


def quiet_kernel_console() -> None:
    subprocess.run(["dmesg", "-n", "1"], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=False)


def network_ready() -> bool:
    route = Path("/proc/net/route")
    try:
        for line in route.read_text(encoding="ascii", errors="ignore").splitlines()[1:]:
            parts = line.split()
            if len(parts) > 2 and parts[1] == "00000000" and int(parts[3], 16) & 0x2:
                return True
    except Exception:
        pass
    return False


def gadget_ready() -> bool:
    if not all(Path(path).exists() for path in ("/dev/g_printer0", "/dev/hidg0", "/dev/hidg1")):
        return False
    for path in (
        "/sys/kernel/config/usb_gadget/rk3588_c0_hid_printer/UDC",
        "/sys/kernel/config/usb_gadget/rk3588_c1_msc/UDC",
    ):
        try:
            if not Path(path).read_text(encoding="ascii", errors="ignore").strip():
                return False
        except Exception:
            return False
    return True


def api_ready(url: str, state_file: str = "") -> bool:
    try:
        if state_file:
            read_state_file(state_file)
        else:
            read_state(url)
        return True
    except Exception:
        return False


def run_boot_check(
    display: Any,
    renderer: AssetRenderer,
    url: str,
    timeout: float,
    state_file: str = "",
) -> None:
    deadline = time.monotonic() + timeout
    last_detail = TEXT["self_check_starting"]
    while time.monotonic() < deadline:
        checks = {
            TEXT["network"]: network_ready(),
            TEXT["service"]: api_ready(url, state_file),
        }
        if not state_file:
            checks["Gadget"] = gadget_ready()
        bad = [name for name, ok in checks.items() if not ok]
        if not bad:
            display.write(renderer.render_boot("ok", TEXT["service_started"]))
            time.sleep(1.6)
            return
        last_detail = TEXT["waiting"] + " " + " / ".join(bad)
        display.write(renderer.render_boot("checking", last_detail))
        time.sleep(0.8)
    display.write(renderer.render_boot("fail", last_detail))
    time.sleep(1.8)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", choices=("fb", "ili9488"), default="fb")
    parser.add_argument("--fb", default="/dev/fb0")
    parser.add_argument("--fb-rotate", type=int, choices=(0, 90, 180, 270), default=0)
    # The verified SPI3 M3 wiring uses GPIO4_A5 for DC and GPIO4_A6 for
    # software CS. RESET is tied high on the display header.
    parser.add_argument("--spidev", default="/dev/spidev3.0")
    parser.add_argument("--dc-gpio", type=int, default=133)  # GPIO4_A5
    parser.add_argument("--reset-gpio", type=int, default=-1)
    parser.add_argument("--cs-gpio", type=int, default=134)  # GPIO4_A6
    parser.add_argument("--bl-gpio", type=int, default=-1)
    parser.add_argument("--spi-speed", type=int, default=4000000)
    parser.add_argument("--rotate", type=int, choices=(0, 90, 180, 270), default=90)
    parser.add_argument("--color-order", choices=("rgb", "bgr"), default="rgb")
    parser.add_argument("--pixel-format", type=int, choices=(16, 18), default=18)
    parser.add_argument("--invert", choices=("on", "off"), default="off")
    parser.add_argument("--url", default="http://127.0.0.1:8080/display/state")
    parser.add_argument("--state-file", default="")
    parser.add_argument("--camera-status-file", default="")
    parser.add_argument("--camera-status-url", default="")
    parser.add_argument("--interval", type=float, default=0.5)
    parser.add_argument("--width", type=int, default=CANVAS_W)
    parser.add_argument("--height", type=int, default=CANVAS_H)
    parser.add_argument("--bpp", type=int, default=32)
    parser.add_argument("--assets-dir", default="/opt/rk3588_gateway/" + TEXT["assets_dir"])
    parser.add_argument("--boot-check-timeout", type=float, default=18)
    args = parser.parse_args()

    quiet_kernel_console()
    hide_console()
    if args.output == "ili9488":
        display = Ili9488Display(
            spidev=args.spidev,
            dc_gpio=args.dc_gpio,
            reset_gpio=args.reset_gpio if args.reset_gpio >= 0 else None,
            cs_gpio=args.cs_gpio if args.cs_gpio >= 0 else None,
            bl_gpio=args.bl_gpio if args.bl_gpio >= 0 else None,
            speed_hz=args.spi_speed,
            rotate=args.rotate,
            color_order=args.color_order,
            pixel_format=args.pixel_format,
            invert=args.invert == "on",
        )
    else:
        display = Framebuffer.open(args.fb, args.width, args.height, args.bpp, args.fb_rotate)
    renderer = AssetRenderer(Path(args.assets_dir))

    run_boot_check(display, renderer, args.url, args.boot_check_timeout, args.state_file)

    last_state: dict[str, Any] = {}
    camera_merger = CameraStatusMerger()
    last_render_signature = ""
    while True:
        error = ""
        try:
            last_state = (
                read_state_file(args.state_file)
                if args.state_file
                else read_state(args.url)
            )
        except Exception as exc:
            error = str(exc) if not last_state else ""
        effective_state = last_state
        if args.camera_status_file or args.camera_status_url:
            try:
                effective_state = camera_merger.merge(
                    last_state,
                    read_camera_status_source(
                        args.camera_status_file,
                        args.camera_status_url,
                    ),
                )
            except Exception:
                pass
        signature = render_signature(effective_state, error)
        if signature != last_render_signature:
            display.write(renderer.render(effective_state, error))
            last_render_signature = signature
        time.sleep(args.interval)


if __name__ == "__main__":
    raise SystemExit(main())
