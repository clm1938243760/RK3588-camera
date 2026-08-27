#!/usr/bin/env python3
from __future__ import print_function

import argparse
import base64
import json
import os
import queue
import re
import subprocess
import tempfile
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer


DEFAULT_DEMO_DIR = "/userdata/aidemo/rknn_PPOCR-System_demo_native"
DEFAULT_WINDOW_DIR = "/userdata/aidemo/window_yolo"
DEFAULT_ICON_DIR = "/userdata/aidemo/icon_match"
DEFAULT_IMAGE_TOOLS_DIR = "/userdata/aidemo/image_tools"
BOX_RE = re.compile(r"^\[(?P<index>\d+)\]\s+@\s+\[(?P<points>.*)\]$")
POINT_RE = re.compile(r"\((-?\d+),\s*(-?\d+)\)")
TEXT_RE = re.compile(r"regconize result:\s*(?P<text>.*),\s*score=(?P<score>[-+0-9.eE]+)")
IMAGE_SIZE_RE = re.compile(r"input image:\s*(?P<width>\d+)\s*x\s*(?P<height>\d+)")
WINDOW_RE = re.compile(
    r"^WINDOW\s+label=(?P<label>\S+)\s+score=(?P<score>[-+0-9.eE]+)\s+box=(?P<box>-?\d+,-?\d+,-?\d+,-?\d+)"
)
ICON_RE = re.compile(
    r"^ICON\s+score=(?P<score>[-+0-9.eE]+)\s+center=(?P<center>null|-?\d+,-?\d+)\s+box=(?P<box>null|-?\d+,-?\d+,-?\d+,-?\d+)"
)
CROP_RE = re.compile(
    r"^CROP\s+box=(?P<box>-?\d+,-?\d+,-?\d+,-?\d+)\s+size=(?P<width>\d+)x(?P<height>\d+)\s+scale=(?P<scale>[-+0-9.eE]+)"
)


class LineWorker(object):
    def __init__(self, cmd, cwd, env, timeout):
        self.cmd = cmd
        self.cwd = cwd
        self.env = env
        self.timeout = timeout
        self.proc = None
        self.lines = queue.Queue()
        self.lock = threading.Lock()
        self.reader = None

    def _reader_loop(self):
        try:
            while True:
                line = self.proc.stdout.readline()
                if not line:
                    break
                self.lines.put(line.rstrip("\r\n"))
        except Exception as exc:
            self.lines.put(json.dumps({"ok": False, "error": "worker reader failed: %s" % exc}))

    def start(self):
        if self.proc is not None and self.proc.poll() is None:
            return
        self.stop()
        self.lines = queue.Queue()
        self.proc = subprocess.Popen(
            self.cmd,
            cwd=self.cwd,
            env=self.env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.DEVNULL,
            universal_newlines=True,
            bufsize=1,
        )
        self.reader = threading.Thread(target=self._reader_loop)
        self.reader.daemon = True
        self.reader.start()
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                line = self.lines.get(timeout=max(0.1, min(1.0, deadline - time.time())))
            except queue.Empty:
                continue
            if line == "READY":
                return
            if line.startswith("{"):
                payload = json.loads(line)
                raise RuntimeError(payload.get("error") or "worker failed before ready")
        self.stop()
        raise RuntimeError("worker startup timeout")

    def stop(self):
        proc = self.proc
        self.proc = None
        if proc is None:
            return
        try:
            if proc.poll() is None and proc.stdin:
                proc.stdin.write("QUIT\n")
                proc.stdin.flush()
        except Exception:
            pass
        try:
            proc.wait(timeout=1.0)
        except Exception:
            try:
                proc.kill()
            except Exception:
                pass

    def request_once(self, line):
        self.start()
        if self.proc is None or self.proc.poll() is not None:
            raise RuntimeError("worker is not running")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()
        deadline = time.time() + self.timeout
        while time.time() < deadline:
            try:
                out = self.lines.get(timeout=max(0.1, min(1.0, deadline - time.time())))
            except queue.Empty:
                continue
            if not out.startswith("{"):
                continue
            payload = json.loads(out)
            if not payload.get("ok", False):
                raise RuntimeError(payload.get("error") or "worker request failed")
            return payload
        self.stop()
        raise RuntimeError("worker request timeout")

    def request(self, line):
        with self.lock:
            try:
                return self.request_once(line)
            except Exception:
                self.stop()
                return self.request_once(line)


class OcrRunner(object):
    def __init__(self, demo_dir, timeout):
        self.demo_dir = os.path.abspath(demo_dir)
        self.timeout = timeout
        self.lock = threading.Lock()
        self.binary = os.path.join(self.demo_dir, "rknn_ppocr_system_demo")
        self.worker_binary = os.path.join(self.demo_dir, "rknn_ppocr_system_worker")
        self.det_model = os.path.join("model", "ppocrv4_det.rknn")
        self.rec_model = os.path.join("model", "ppocrv4_rec.rknn")
        self.worker = None

    def check_ready(self):
        missing = []
        det_model = os.path.join(self.demo_dir, self.det_model)
        rec_model = os.path.join(self.demo_dir, self.rec_model)
        if os.path.exists(self.worker_binary):
            required = (self.worker_binary, det_model, rec_model)
        else:
            required = (self.binary, det_model, rec_model)
        for path in required:
            if not os.path.exists(path):
                missing.append(path)
        return missing

    def worker_ready(self):
        return os.path.exists(self.worker_binary)

    def run_file_with_worker(self, image_path):
        return self.run_worker_line(image_path)

    def run_worker_line(self, line):
        env = os.environ.copy()
        lib_path = os.path.join(self.demo_dir, "lib")
        old_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = lib_path + (":" + old_ld if old_ld else "")
        if self.worker is None:
            self.worker = LineWorker(
                [
                    self.worker_binary,
                    os.path.join(self.demo_dir, self.det_model),
                    os.path.join(self.demo_dir, self.rec_model),
                ],
                self.demo_dir,
                env,
                self.timeout,
            )
        return self.worker.request(line)

    def run_file_crop(self, image_path, box, margin, scale):
        if not self.worker_ready():
            raise RuntimeError("ocr worker is not available")
        if scale <= 0:
            scale = 1.0
        line = "%s\t%d\t%d\t%d\t%d\t%d\t%f" % (
            image_path,
            int(box[0]),
            int(box[1]),
            int(box[2]),
            int(box[3]),
            int(margin),
            float(scale),
        )
        return self.run_worker_line(line)

    def run_file(self, image_path):
        missing = self.check_ready()
        if missing:
            raise RuntimeError("missing runtime files: " + ", ".join(missing))
        if self.worker_ready():
            return self.run_file_with_worker(image_path)

        start = time.time()
        env = os.environ.copy()
        lib_path = os.path.join(self.demo_dir, "lib")
        old_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = lib_path + (":" + old_ld if old_ld else "")
        cmd = [self.binary, self.det_model, self.rec_model, image_path]
        with self.lock:
            proc = subprocess.run(
                cmd,
                cwd=self.demo_dir,
                env=env,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout,
            )
        elapsed_ms = int(round((time.time() - start) * 1000))
        raw = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError("ppocr demo exit %s: %s" % (proc.returncode, raw[-2000:]))
        parsed = parse_demo_output(raw)
        parsed["elapsed_ms"] = elapsed_ms
        parsed["raw_tail"] = raw[-2000:]
        return parsed

    def run(self, image_bytes):
        fd, image_path = tempfile.mkstemp(prefix="ppocr_", suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(image_bytes)
            return self.run_file(image_path)
        finally:
            try:
                os.unlink(image_path)
            except OSError:
                pass


class WindowRunner(object):
    def __init__(self, window_dir, timeout, conf, iou, max_det):
        self.window_dir = os.path.abspath(window_dir)
        self.timeout = timeout
        self.conf = conf
        self.iou = iou
        self.max_det = max_det
        self.lock = threading.Lock()
        self.binary = os.path.join(self.window_dir, "window_yolo_rknn")
        self.worker_binary = os.path.join(self.window_dir, "window_yolo_worker")
        self.model = os.path.join(self.window_dir, "window_yolov8n_640_add9.rknn")
        self.worker = None

    def check_ready(self):
        missing = []
        if os.path.exists(self.worker_binary):
            required = (self.worker_binary, self.model)
        else:
            required = (self.binary, self.model)
        for path in required:
            if not os.path.exists(path):
                missing.append(path)
        return missing

    def worker_ready(self):
        return os.path.exists(self.worker_binary)

    def run_file_with_worker(self, image_path):
        env = os.environ.copy()
        old_ld = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = "/usr/lib:/usr/lib/aarch64-linux-gnu" + (":" + old_ld if old_ld else "")
        if self.worker is None:
            self.worker = LineWorker([self.worker_binary, self.model], self.window_dir, env, self.timeout)
        return self.worker.request(
            "%s\t%s\t%s\t%s" % (image_path, self.conf, self.iou, self.max_det)
        )

    def run(self, image_bytes):
        missing = self.check_ready()
        if missing:
            raise RuntimeError("missing window runtime files: " + ", ".join(missing))

        fd, image_path = tempfile.mkstemp(prefix="window_", suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(image_bytes)
            if self.worker_ready():
                return self.run_file_with_worker(image_path)
            start = time.time()
            env = os.environ.copy()
            old_ld = env.get("LD_LIBRARY_PATH", "")
            env["LD_LIBRARY_PATH"] = "/usr/lib:/usr/lib/aarch64-linux-gnu" + (":" + old_ld if old_ld else "")
            cmd = [
                self.binary,
                self.model,
                image_path,
                str(self.conf),
                str(self.iou),
                str(self.max_det),
            ]
            with self.lock:
                proc = subprocess.run(
                    cmd,
                    cwd=self.window_dir,
                    env=env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=self.timeout,
                )
            elapsed_ms = int(round((time.time() - start) * 1000))
            raw = proc.stdout.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                raise RuntimeError("window demo exit %s: %s" % (proc.returncode, raw[-2000:]))
            return {
                "windows": parse_window_output(raw),
                "elapsed_ms": elapsed_ms,
                "raw_tail": raw[-2000:],
            }
        finally:
            try:
                os.unlink(image_path)
            except OSError:
                pass


class CropRunner(object):
    def __init__(self, tools_dir, timeout, margin, scale):
        self.tools_dir = os.path.abspath(tools_dir)
        self.timeout = timeout
        self.margin = margin
        self.scale = scale
        self.binary = os.path.join(self.tools_dir, "image_crop_resize")
        self.lock = threading.Lock()

    def check_ready(self):
        if os.path.exists(self.binary):
            return []
        return [self.binary]

    def crop(self, source_path, output_path, box):
        missing = self.check_ready()
        if missing:
            raise RuntimeError("missing crop runtime files: " + ", ".join(missing))
        cmd = [
            self.binary,
            source_path,
            output_path,
            str(int(box[0])),
            str(int(box[1])),
            str(int(box[2])),
            str(int(box[3])),
            str(int(self.margin)),
            str(float(self.scale)),
        ]
        with self.lock:
            proc = subprocess.run(
                cmd,
                cwd=self.tools_dir,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                timeout=self.timeout,
            )
        raw = proc.stdout.decode("utf-8", errors="replace")
        if proc.returncode != 0:
            raise RuntimeError("crop tool exit %s: %s" % (proc.returncode, raw[-1000:]))
        return parse_crop_output(raw)


class IconTemplateRunner(object):
    def __init__(self, icon_dir, timeout):
        self.icon_dir = os.path.abspath(icon_dir)
        self.timeout = timeout
        self.lock = threading.Lock()
        self.binary = os.path.join(self.icon_dir, "icon_template_match")
        self.templates = [
            {
                "software": "人体成分分析仪",
                "path": os.path.join(self.icon_dir, "bodypass.jpg"),
                "threshold": 0.65,
                "offset": [43, 31],
            },
            {
                "software": "BodyPass",
                "path": os.path.join(self.icon_dir, "bodypass_bodypass.jpg"),
                "threshold": 0.90,
                "offset": [43, 31],
            },
            {
                "software": "OCT",
                "path": os.path.join(self.icon_dir, "oct_icon.jpg"),
                "threshold": 0.65,
                "offset": [23, 24],
            },
            {
                "software": "盆底肌",
                "path": os.path.join(self.icon_dir, "pelvic_floor_icon.jpg"),
                "threshold": 0.90,
                "offset": [41, 31],
            },
            {
                "software": "esophageal_motility",
                "path": os.path.join(self.icon_dir, "esophageal_motility_icon.jpg"),
                "threshold": 0.88,
                "offset": [31, 18],
            },
            {
                "software": "\u98df\u9053\u52a8\u529b",
                "path": os.path.join(self.icon_dir, "esophageal_motility_icon.jpg"),
                "threshold": 0.88,
                "offset": [31, 18],
            }
        ]

    def check_ready(self):
        missing = []
        if not os.path.exists(self.binary):
            missing.append(self.binary)
        for template in self.templates:
            if not os.path.exists(template["path"]):
                missing.append(template["path"])
        return missing

    def find_template(self, software):
        wanted = compact_text(software)
        for template in self.templates:
            name = compact_text(template["software"])
            if wanted == name or wanted in name or name in wanted:
                return template
        return None

    def run(self, image_bytes, software):
        template = self.find_template(software)
        if template is None:
            return None
        missing = self.check_ready()
        if missing:
            raise RuntimeError("missing icon runtime files: " + ", ".join(missing))

        fd, image_path = tempfile.mkstemp(prefix="icon_", suffix=".jpg")
        try:
            with os.fdopen(fd, "wb") as handle:
                handle.write(image_bytes)
            cmd = [
                self.binary,
                image_path,
                template["path"],
                str(template["threshold"]),
                str(template["offset"][0]),
                str(template["offset"][1]),
            ]
            with self.lock:
                proc = subprocess.run(
                    cmd,
                    cwd=self.icon_dir,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.STDOUT,
                    timeout=self.timeout,
                )
            raw = proc.stdout.decode("utf-8", errors="replace")
            if proc.returncode != 0:
                raise RuntimeError("icon template exit %s: %s" % (proc.returncode, raw[-1000:]))
            return parse_icon_output(raw, template)
        finally:
            try:
                os.unlink(image_path)
            except OSError:
                pass


def parse_icon_output(raw, template):
    for line in raw.splitlines():
        match = ICON_RE.match(line.strip())
        if not match:
            continue
        center_text = match.group("center")
        box_text = match.group("box")
        try:
            score = float(match.group("score"))
        except ValueError:
            score = None
        center = None if center_text == "null" else [int(part) for part in center_text.split(",")]
        box = None if box_text == "null" else [int(part) for part in box_text.split(",")]
        return {
            "center": center,
            "box": box,
            "template_score": score,
            "matched_template": template["software"] if center is not None else None,
        }
    return None


def parse_crop_output(raw):
    for line in raw.splitlines():
        match = CROP_RE.match(line.strip())
        if not match:
            continue
        box = [int(part) for part in match.group("box").split(",")]
        return {
            "box": box,
            "width": int(match.group("width")),
            "height": int(match.group("height")),
            "scale": float(match.group("scale")),
            "raw_tail": raw[-1000:],
        }
    return {"box": None, "raw_tail": raw[-1000:]}


def parse_window_output(raw):
    windows = []
    for line in raw.splitlines():
        match = WINDOW_RE.match(line.strip())
        if not match:
            continue
        box = [int(part) for part in match.group("box").split(",")]
        try:
            score = float(match.group("score"))
        except ValueError:
            score = None
        windows.append(
            {
                "label": match.group("label"),
                "box": box,
                "score": score,
                "ocr": [],
            }
        )
    return windows


def parse_demo_output(raw):
    items = []
    pending = None
    image_size = None
    for line in raw.splitlines():
        size_match = IMAGE_SIZE_RE.search(line)
        if size_match:
            image_size = {
                "width": int(size_match.group("width")),
                "height": int(size_match.group("height")),
            }

        box_match = BOX_RE.match(line.strip())
        if box_match:
            points = [
                [int(match.group(1)), int(match.group(2))]
                for match in POINT_RE.finditer(box_match.group("points"))
            ]
            if points:
                xs = [point[0] for point in points]
                ys = [point[1] for point in points]
                pending = {
                    "index": int(box_match.group("index")),
                    "polygon": points,
                    "box": [min(xs), min(ys), max(xs), max(ys)],
                    "center": [
                        int(round(sum(xs) / float(len(xs)))),
                        int(round(sum(ys) / float(len(ys)))),
                    ],
                }
            continue

        text_match = TEXT_RE.search(line)
        if text_match and pending is not None:
            item = dict(pending)
            item["text"] = text_match.group("text").strip()
            try:
                item["score"] = float(text_match.group("score"))
            except ValueError:
                item["score"] = None
            items.append(item)
            pending = None

    return {"ok": True, "ocr": items, "image_size": image_size}


def jpeg_size(image_bytes):
    if len(image_bytes) < 4 or image_bytes[0:2] != b"\xff\xd8":
        return None
    i = 2
    while i + 9 < len(image_bytes):
        if image_bytes[i] != 0xFF:
            i += 1
            continue
        while i < len(image_bytes) and image_bytes[i] == 0xFF:
            i += 1
        if i >= len(image_bytes):
            break
        marker = image_bytes[i]
        i += 1
        if marker in (0xD8, 0xD9):
            continue
        if marker == 0xDA:
            break
        if i + 2 > len(image_bytes):
            break
        length = (image_bytes[i] << 8) + image_bytes[i + 1]
        if length < 2 or i + length > len(image_bytes):
            break
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7, 0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if length >= 7:
                height = (image_bytes[i + 3] << 8) + image_bytes[i + 4]
                width = (image_bytes[i + 5] << 8) + image_bytes[i + 6]
                return {"width": width, "height": height}
        i += length
    return None


def offset_ocr_items(ocr_items, offset_x, offset_y, scale):
    shifted = []
    if not scale:
        scale = 1.0
    for item in ocr_items:
        updated = dict(item)
        if isinstance(item.get("center"), list) and len(item["center"]) == 2:
            updated["center"] = [
                int(round(item["center"][0] / scale + offset_x)),
                int(round(item["center"][1] / scale + offset_y)),
            ]
        if isinstance(item.get("box"), list) and len(item["box"]) == 4:
            updated["box"] = [
                int(round(item["box"][0] / scale + offset_x)),
                int(round(item["box"][1] / scale + offset_y)),
                int(round(item["box"][2] / scale + offset_x)),
                int(round(item["box"][3] / scale + offset_y)),
            ]
        if isinstance(item.get("polygon"), list):
            polygon = []
            for point in item["polygon"]:
                if isinstance(point, list) and len(point) == 2:
                    polygon.append([
                        int(round(point[0] / scale + offset_x)),
                        int(round(point[1] / scale + offset_y)),
                    ])
            updated["polygon"] = polygon
        shifted.append(updated)
    return shifted


def point_in_box(point, box, margin=4):
    if not isinstance(point, list) or len(point) != 2:
        return False
    x, y = point
    if not isinstance(x, (int, float)) or not isinstance(y, (int, float)):
        return False
    return box[0] - margin <= x <= box[2] + margin and box[1] - margin <= y <= box[3] + margin


def assign_ocr_to_windows(ocr_items, windows):
    for window in windows:
        box = window.get("box")
        if not isinstance(box, list) or len(box) != 4:
            window["ocr"] = []
            continue
        window["ocr"] = [
            item for item in ocr_items if point_in_box(item.get("center"), box)
        ]
    return windows


def expanded_crop_box(image_size, box, margin):
    width = int(image_size.get("width") or 0)
    height = int(image_size.get("height") or 0)
    x1 = max(0, min(int(box[0]) - int(margin), max(0, width - 1)))
    y1 = max(0, min(int(box[1]) - int(margin), max(0, height - 1)))
    x2 = max(0, min(int(box[2]) + int(margin), width))
    y2 = max(0, min(int(box[3]) + int(margin), height))
    return [x1, y1, x2, y2]


def make_window_crop_response(image_bytes, source_path, window_result, ocr_runner, crop_runner):
    image_size = jpeg_size(image_bytes) or {"width": 0, "height": 0}
    windows = window_result.get("windows") or []
    response = {
        "ok": True,
        "ocr": [],
        "image_size": image_size,
        "elapsed_ms": 0,
        "windows": [],
        "window_elapsed_ms": window_result.get("elapsed_ms"),
        "window_raw_tail": window_result.get("raw_tail"),
    }

    if not windows:
        parsed = ocr_runner.run_file(source_path)
        parsed["windows"] = [
            {
                "label": None,
                "box": [0, 0, int(image_size.get("width") or 0), int(image_size.get("height") or 0)],
                "ocr": parsed.get("ocr") or [],
            }
        ]
        parsed["window_elapsed_ms"] = window_result.get("elapsed_ms")
        parsed["window_raw_tail"] = window_result.get("raw_tail")
        if not parsed.get("image_size"):
            parsed["image_size"] = image_size
        return parsed

    total_ocr_ms = 0
    raw_tails = []
    for index, window in enumerate(windows):
        box = window.get("box")
        if not isinstance(box, list) or len(box) != 4:
            continue

        if ocr_runner.worker_ready():
            crop_box = expanded_crop_box(image_size, box, crop_runner.margin)
            scale = crop_runner.scale if crop_runner.scale > 0 else 1.0
            parsed = ocr_runner.run_file_crop(source_path, box, crop_runner.margin, scale)
            total_ocr_ms += int(parsed.get("elapsed_ms") or 0)
            raw_tails.append(parsed.get("raw_tail") or "")
            ocr_items = offset_ocr_items(parsed.get("ocr") or [], crop_box[0], crop_box[1], scale)
            output_window = dict(window)
            output_window["ocr"] = ocr_items
            output_window["crop_box"] = crop_box
            response["windows"].append(output_window)
            response["ocr"].extend(ocr_items)
            continue

        fd, crop_path = tempfile.mkstemp(prefix="window_crop_%d_" % index, suffix=".jpg")
        os.close(fd)
        try:
            crop_meta = crop_runner.crop(source_path, crop_path, box)
            crop_box = crop_meta.get("box") or box
            scale = crop_meta.get("scale") or 1.0
            parsed = ocr_runner.run_file(crop_path)
            total_ocr_ms += int(parsed.get("elapsed_ms") or 0)
            raw_tails.append(parsed.get("raw_tail") or "")
            ocr_items = offset_ocr_items(parsed.get("ocr") or [], crop_box[0], crop_box[1], scale)
            output_window = dict(window)
            output_window["ocr"] = ocr_items
            output_window["crop_box"] = crop_box
            response["windows"].append(output_window)
            response["ocr"].extend(ocr_items)
        finally:
            try:
                os.unlink(crop_path)
            except OSError:
                pass

    response["elapsed_ms"] = total_ocr_ms
    response["raw_tail"] = "\n".join(raw_tails)[-2000:]
    return response


def make_window_response(parsed, window_result=None):
    image_size = parsed.get("image_size") or {}
    width = int(image_size.get("width") or 0)
    height = int(image_size.get("height") or 0)
    ocr_items = parsed.get("ocr") or []
    response = dict(parsed)
    if window_result and window_result.get("windows"):
        windows = assign_ocr_to_windows(ocr_items, window_result["windows"])
    else:
        windows = [
            {
                "label": None,
                "box": [0, 0, width, height],
                "ocr": ocr_items,
            }
        ]
    response["windows"] = windows
    if window_result:
        response["window_elapsed_ms"] = window_result.get("elapsed_ms")
        response["window_raw_tail"] = window_result.get("raw_tail")
    return response


def decode_image_payload(handler):
    length = int(handler.headers.get("Content-Length") or "0")
    if length <= 0:
        raise ValueError("empty request body")
    body = handler.rfile.read(length)
    content_type = (handler.headers.get("Content-Type") or "").split(";")[0].strip().lower()

    if content_type == "application/json" or not content_type:
        payload = json.loads(body.decode("utf-8"))
        if not isinstance(payload, dict):
            raise ValueError("json body must be an object")
        value = payload.get("image_base64") or payload.get("image") or payload.get("imageBase64")
        if not isinstance(value, str) or not value:
            raise ValueError("missing image_base64")
        if "," in value and value.split(",", 1)[0].startswith("data:"):
            value = value.split(",", 1)[1]
        return base64.b64decode(value), payload

    if content_type.startswith("image/") or content_type == "application/octet-stream":
        return body, {}

    raise ValueError("unsupported content type: " + content_type)


def decode_image_request(handler):
    return decode_image_payload(handler)[0]


def compact_text(text):
    return "".join(str(text).split()).lower()


def find_software_center(ocr_items, software):
    wanted = compact_text(software)
    if not wanted:
        return None, None

    candidates = []
    for item in ocr_items:
        text = str(item.get("text") or "").strip()
        center = item.get("center")
        if not text or not center:
            continue
        compact = compact_text(text)
        if compact == wanted:
            candidates.append((3, item))
        elif wanted in compact:
            candidates.append((2, item))
        elif compact in wanted and len(compact) >= 2:
            candidates.append((1, item))

    if not candidates:
        return None, None
    candidates.sort(key=lambda pair: (pair[0], float(pair[1].get("score") or 0.0)), reverse=True)
    item = candidates[0][1]
    return item.get("center"), item.get("text")


def write_json(handler, status, payload):
    raw = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    handler.send_response(status)
    handler.send_header("Content-Type", "application/json; charset=utf-8")
    handler.send_header("Content-Length", str(len(raw)))
    handler.end_headers()
    handler.wfile.write(raw)


class OcrHandler(BaseHTTPRequestHandler):
    server_version = "PPOCRRknnHTTP/0.1"

    def do_GET(self):
        path = self.path.split("?", 1)[0]
        if path == "/health":
            ocr_missing = self.server.ocr_runner.check_ready()
            window_missing = self.server.window_runner.check_ready()
            icon_missing = self.server.icon_runner.check_ready()
            crop_missing = self.server.crop_runner.check_ready()
            missing = ocr_missing + window_missing + icon_missing + crop_missing
            write_json(
                self,
                200 if not missing else 503,
                {
                    "ok": not bool(missing),
                    "backend": "rk3588_vision_rknn",
                    "demo_dir": self.server.ocr_runner.demo_dir,
                    "window_dir": self.server.window_runner.window_dir,
                    "icon_dir": self.server.icon_runner.icon_dir,
                    "image_tools_dir": self.server.crop_runner.tools_dir,
                    "ocr_missing": ocr_missing,
                    "window_missing": window_missing,
                    "icon_missing": icon_missing,
                    "crop_missing": crop_missing,
                },
            )
            return
        write_json(self, 404, {"ok": False, "error": "not found"})

    def do_POST(self):
        path = self.path.split("?", 1)[0]
        if path not in ("/ocr", "/window/detect", "/detect_window", "/icon/locate", "/locate_icon"):
            write_json(self, 404, {"ok": False, "error": "not found"})
            return
        try:
            image_bytes, payload = decode_image_payload(self)
            if path in ("/window/detect", "/detect_window"):
                fd, source_path = tempfile.mkstemp(prefix="window_source_", suffix=".jpg")
                try:
                    with os.fdopen(fd, "wb") as handle:
                        handle.write(image_bytes)
                    window_result = self.server.window_runner.run(image_bytes)
                    parsed = make_window_crop_response(
                        image_bytes,
                        source_path,
                        window_result,
                        self.server.ocr_runner,
                        self.server.crop_runner,
                    )
                finally:
                    try:
                        os.unlink(source_path)
                    except OSError:
                        pass
            else:
                parsed = self.server.ocr_runner.run(image_bytes)

            if path in ("/icon/locate", "/locate_icon"):
                software = str(payload.get("software") or payload.get("label") or "").strip()
                if not software:
                    write_json(self, 400, {"ok": False, "error": "missing software"})
                    return
                icon_result = None
                center = None
                matched_text = None
                has_template = self.server.icon_runner.find_template(software) is not None
                if has_template:
                    icon_result = self.server.icon_runner.run(image_bytes, software)
                    if icon_result:
                        center = icon_result.get("center")
                else:
                    center, matched_text = find_software_center(parsed.get("ocr") or [], software)
                parsed = {
                    "ok": True,
                    "center": center,
                    "matched_text": matched_text,
                    "ocr_count": len(parsed.get("ocr") or []),
                    "elapsed_ms": parsed.get("elapsed_ms"),
                }
                if icon_result:
                    parsed.update(icon_result)
            write_json(self, 200, parsed)
        except Exception as exc:
            write_json(self, 500, {"ok": False, "error": str(exc)})

    def log_message(self, fmt, *args):
        print("%s - - [%s] %s" % (self.address_string(), self.log_date_time_string(), fmt % args))


def parse_args():
    parser = argparse.ArgumentParser(description="Tiny HTTP wrapper for RKNN PPOCR demo")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--demo-dir", default=DEFAULT_DEMO_DIR)
    parser.add_argument("--window-dir", default=DEFAULT_WINDOW_DIR)
    parser.add_argument("--icon-dir", default=DEFAULT_ICON_DIR)
    parser.add_argument("--image-tools-dir", default=DEFAULT_IMAGE_TOOLS_DIR)
    parser.add_argument("--timeout", type=float, default=30.0)
    parser.add_argument("--window-conf", type=float, default=0.25)
    parser.add_argument("--window-iou", type=float, default=0.45)
    parser.add_argument("--window-max-det", type=int, default=50)
    parser.add_argument("--crop-margin", type=int, default=8)
    parser.add_argument("--crop-scale", type=float, default=1.0)
    return parser.parse_args()


def main():
    args = parse_args()
    ocr_runner = OcrRunner(args.demo_dir, args.timeout)
    window_runner = WindowRunner(
        args.window_dir,
        args.timeout,
        args.window_conf,
        args.window_iou,
        args.window_max_det,
    )
    icon_runner = IconTemplateRunner(args.icon_dir, args.timeout)
    crop_runner = CropRunner(args.image_tools_dir, args.timeout, args.crop_margin, args.crop_scale)
    server = ThreadingHTTPServer((args.host, args.port), OcrHandler)
    server.ocr_runner = ocr_runner
    server.window_runner = window_runner
    server.icon_runner = icon_runner
    server.crop_runner = crop_runner
    missing = (
        ocr_runner.check_ready()
        + window_runner.check_ready()
        + icon_runner.check_ready()
        + crop_runner.check_ready()
    )
    if missing:
        print("warning: missing runtime files: " + ", ".join(missing))
    print("listening on http://%s:%d" % (args.host, args.port))
    server.serve_forever()


if __name__ == "__main__":
    main()
