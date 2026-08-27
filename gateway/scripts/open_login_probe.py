#!/usr/bin/env python3
"""Standalone HDMI vision probe: open app icon, detect login window, click login."""

from __future__ import annotations

import argparse
import base64
import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any


DEFAULT_ICON_ENDPOINT = "http://127.0.0.1:5002/icon/locate"
DEFAULT_WINDOW_ENDPOINT = "http://127.0.0.1:5002/window/detect"


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Open app and click login via HDMI vision APIs.")
    parser.add_argument("--device", default="/dev/video40", help="HDMI RX V4L2 device")
    parser.add_argument("--mouse", default="/dev/hidg1", help="USB HID absolute mouse gadget")
    parser.add_argument("--workdir", default="/tmp/rk3588-open-login-probe", help="temporary image directory")
    parser.add_argument("--icon-endpoint", default=DEFAULT_ICON_ENDPOINT)
    parser.add_argument("--window-endpoint", default=DEFAULT_WINDOW_ENDPOINT)
    parser.add_argument("--software", default="人体成分分析仪", help="software name sent to /icon/locate")
    parser.add_argument("--wait-after-open", type=float, default=2.5)
    parser.add_argument("--wait-after-action", type=float, default=1.0)
    parser.add_argument("--wait-after-start", type=float, default=3.0)
    parser.add_argument("--analysis-wait", type=float, default=1.0)
    parser.add_argument("--max-runtime", type=float, default=300.0, help="maximum runtime seconds; <=0 disables")
    parser.add_argument("--width", type=int, default=1920)
    parser.add_argument("--height", type=int, default=1080)
    parser.add_argument("--login-label", default="0")
    parser.add_argument("--login-text", default="登录")
    parser.add_argument("--timeout", type=float, default=15.0)
    parser.add_argument("--dry-run", action="store_true", help="print actions without writing /dev/hidg1")
    return parser.parse_args()


def capture_jpeg(device: str, output: Path, timeout: float) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = [
        "gst-launch-1.0",
        "-q",
        "-e",
        "v4l2src",
        f"device={device}",
        "num-buffers=1",
        "!",
        "video/x-raw,format=BGR,width=1920,height=1080,framerate=60/1",
        "!",
        "videoconvert",
        "!",
        "jpegenc",
        "quality=90",
        "!",
        "filesink",
        f"location={output}",
    ]
    subprocess.run(cmd, check=True, timeout=timeout)
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError(f"capture failed or empty image: {output}")


def build_image_body(image_base64: str, extra: dict[str, Any] | None = None) -> bytes:
    payload: dict[str, Any] = {"image_base64": image_base64}
    if extra:
        payload.update(extra)
    return json.dumps(payload, ensure_ascii=False).encode("utf-8")


def post_image(
    endpoint: str,
    image_path: Path,
    timeout: float,
    extra: dict[str, Any] | None = None,
) -> dict[str, Any]:
    image_base64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    body = build_image_body(image_base64, extra)
    request = urllib.request.Request(
        endpoint,
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read().decode("utf-8", errors="replace")
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{endpoint} HTTP {exc.code}: {error_body}") from exc
    except urllib.error.URLError as exc:
        raise RuntimeError(f"{endpoint} request failed: {exc}") from exc

    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{endpoint} returned non-json: {raw[:500]}") from exc
    if not isinstance(parsed, dict):
        raise RuntimeError(f"{endpoint} returned non-object json: {parsed!r}")
    return parsed


def extract_center(response: dict[str, Any], key: str) -> tuple[int, int] | None:
    center = response.get(key)
    if center is None and key == "center" and isinstance(response.get("box"), list):
        box = response["box"]
        if len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
            center = [(box[0] + box[2]) / 2, (box[1] + box[3]) / 2]
    if not isinstance(center, list) or len(center) != 2:
        return None
    x, y = center
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return None
    return int(round(x)), int(round(y))


def find_ocr_center(response: dict[str, Any], text: str) -> tuple[int, int] | None:
    ocr = response.get("ocr")
    if not isinstance(ocr, list):
        return None
    for item in ocr:
        if not isinstance(item, dict):
            continue
        if str(item.get("text", "")).strip() != text:
            continue
        center = extract_center(item, "center")
        if center is not None:
            return center
    return None


def ocr_texts(response: dict[str, Any]) -> list[str]:
    ocr = response.get("ocr")
    if not isinstance(ocr, list):
        return []
    texts = []
    for item in ocr:
        if isinstance(item, dict):
            texts.append(str(item.get("text", "")).strip())
    return texts


def ocr_contains(response: dict[str, Any], text: str) -> bool:
    return any(text in item for item in ocr_texts(response))


def is_loading(response: dict[str, Any]) -> bool:
    loading_words = ("加载", "正在加载", "请稍候", "请稍等", "处理中", "等待")
    return any(any(word in item for word in loading_words) for item in ocr_texts(response))


def should_click_login(response: dict[str, Any], login_label: str = "0") -> bool:
    return str(response.get("label")) == str(login_label)


def label_value(response: dict[str, Any]) -> str | None:
    label = response.get("label")
    if label is None:
        return None
    label = str(label).strip()
    if not label or label.lower() == "none":
        return None
    return label


def decide_action(response: dict[str, Any]) -> tuple[str, str | None]:
    label = label_value(response)
    if label is None:
        if is_loading(response):
            return "wait", None
        return "open", None
    if label == "0":
        return "click_text", "登录"
    if label == "1":
        if ocr_contains(response, "未选择患者"):
            return "click_text", "新建患者"
        if ocr_contains(response, "就绪"):
            return "click_text", "开始检查"
        return "wait", None
    if label == "2":
        return "scanner_stage", None
    if label == "3":
        if ocr_contains(response, "检查完成"):
            return "analysis", None
        return "wait", None
    if label in {"4", "5"}:
        return "click_text", "确定"
    return "wait", None


def decide_after_analysis(response: dict[str, Any]) -> tuple[str, str | None]:
    label = label_value(response)
    if label == "4":
        return "click_text", "确定"
    if label == "5":
        return "click_text", "确定"
    if label == "3" and ocr_contains(response, "检查完成"):
        return "finish", None
    return "wait", None


def close_center(response: dict[str, Any]) -> tuple[int, int] | None:
    for text in ("×", "X", "x"):
        center = find_ocr_center(response, text)
        if center is not None:
            return center
    box = response.get("box")
    if isinstance(box, list) and len(box) == 4 and all(isinstance(v, (int, float)) for v in box):
        return int(round(box[2] - 20)), int(round(box[1] + 15))
    return None


def scale_abs(value: int, size: int) -> int:
    value = max(0, min(size - 1, int(value)))
    return int(round(value * 32767 / (size - 1)))


def mouse_report(x: int, y: int, button: int, width: int, height: int) -> bytes:
    ax = scale_abs(x, width)
    ay = scale_abs(y, height)
    return bytes([button & 0x07, ax & 0xFF, (ax >> 8) & 0xFF, ay & 0xFF, (ay >> 8) & 0xFF])


def write_mouse(device: str, report: bytes, dry_run: bool) -> None:
    if dry_run:
        print(f"dry-run mouse report: {report.hex()}")
        return
    fd = os.open(device, os.O_WRONLY | os.O_NONBLOCK)
    try:
        os.write(fd, report)
    finally:
        os.close(fd)


def click(device: str, x: int, y: int, width: int, height: int, dry_run: bool, double: bool = False) -> None:
    print(f"{'double-click' if double else 'click'} x={x} y={y}")
    write_mouse(device, mouse_report(x, y, 0, width, height), dry_run)
    time.sleep(0.08)
    for index in range(2 if double else 1):
        if index:
            time.sleep(0.12)
        write_mouse(device, mouse_report(x, y, 1, width, height), dry_run)
        time.sleep(0.08)
        write_mouse(device, mouse_report(x, y, 0, width, height), dry_run)


def click_ocr_text(args: argparse.Namespace, response: dict[str, Any], text: str) -> bool:
    center = find_ocr_center(response, text)
    if center is None:
        print(f"OCR text not found: {text}", file=sys.stderr)
        return False
    click(args.mouse, center[0], center[1], args.width, args.height, args.dry_run)
    return True


def click_close(args: argparse.Namespace, response: dict[str, Any]) -> bool:
    center = close_center(response)
    if center is None:
        print("close button not found", file=sys.stderr)
        return False
    click(args.mouse, center[0], center[1], args.width, args.height, args.dry_run)
    return True


def detect_window(args: argparse.Namespace, image_path: Path) -> dict[str, Any]:
    print(f"capture window image: {image_path}")
    capture_jpeg(args.device, image_path, args.timeout)
    response = post_image(args.window_endpoint, image_path, args.timeout)
    print("window response:", json.dumps(response, ensure_ascii=False))
    return response


def open_app(args: argparse.Namespace, image_path: Path) -> bool:
    print(f"capture open image: {image_path}")
    capture_jpeg(args.device, image_path, args.timeout)
    response = post_image(args.icon_endpoint, image_path, args.timeout, {"software": args.software})
    print("icon response:", json.dumps(response, ensure_ascii=False))
    center = extract_center(response, "center")
    if center is None:
        print("icon center not found", file=sys.stderr)
        return False
    click(args.mouse, center[0], center[1], args.width, args.height, args.dry_run, double=True)
    print(f"wait {args.wait_after_open:.1f}s")
    time.sleep(args.wait_after_open)
    return True


def handle_analysis(args: argparse.Namespace, response: dict[str, Any], workdir: Path, capture_index: int) -> int | None:
    if not click_ocr_text(args, response, "数据分析"):
        return 3
    print(f"wait {args.analysis_wait:.1f}s after data analysis")
    time.sleep(args.analysis_wait)
    next_response = detect_window(args, workdir / f"after_analysis_{capture_index}.jpg")
    action, target = decide_after_analysis(next_response)
    print(f"after_analysis_action={action} target={target}")
    if action == "click_text" and target:
        click_ocr_text(args, next_response, target)
        return 0
    if action == "finish":
        print("analysis finished: no follow-up dialog/state change after data analysis")
        return 0
    return None


def main() -> int:
    args = parse_args()
    workdir = Path(args.workdir)
    started_at = time.monotonic()
    capture_index = 0

    while True:
        if args.max_runtime > 0 and time.monotonic() - started_at > args.max_runtime:
            print(f"max runtime reached: {args.max_runtime:.1f}s")
            return 6

        capture_index += 1
        response = detect_window(args, workdir / f"window_{capture_index}.jpg")
        action, target = decide_action(response)
        print(f"action={action} target={target}")

        if action == "open":
            if not open_app(args, workdir / f"open_{capture_index}.jpg"):
                return 2
            continue

        if action == "click_text" and target:
            if not click_ocr_text(args, response, target):
                return 3
            if target == "开始检查":
                print(f"wait {args.wait_after_start:.1f}s after start")
                time.sleep(args.wait_after_start)
            else:
                time.sleep(args.wait_after_action)
            continue

        if action == "scanner_stage":
            print("label=2 detected; new patient window is ready for the existing scanner auto input flow")
            return 0

        if action == "analysis":
            result = handle_analysis(args, response, workdir, capture_index)
            if result is not None:
                return result
            continue

        time.sleep(args.wait_after_action)


if __name__ == "__main__":
    raise SystemExit(main())
