from __future__ import annotations

import asyncio
import base64
import json
import logging
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Callable

from .compat import to_thread, unlink_missing_ok


LOGGER = logging.getLogger(__name__)
DEFAULT_ICON_ENDPOINT = "http://127.0.0.1:5002/icon/locate"
DEFAULT_WINDOW_ENDPOINT = "http://127.0.0.1:5002/window/detect"
MIN_CAPTURE_FRAME_BYTES = 64 * 1024
MAX_CAPTURE_ATTEMPTS = 4
CAPTURE_RETRY_DELAY_SECONDS = 0.2
YES_BUTTON_TEXTS = ("是(Y)", "是（Y）")
CONFIRM_BUTTON_TEXTS = ("确认", "确定")
PDF_REPORT_PROMPT_TEXT = "是否生成PDF报告"
START_CHECK_TEXT = "开始检查"
CHECK_DONE_TEXT = "检查完成"
ANALYSIS_TEXT = "数据分析"
ANALYSIS_NO_RESPONSE_WAIT_SECONDS = 1.5
LOGIN_TEXT = "登录"
LOGIN_TITLE_TEXT = "用户登录"
LOGIN_BUTTON_TITLE_OFFSET = (68, 245)
USERNAME_TEXT = "用户名"
PASSWORD_TEXT = "密码"
NEW_PATIENT_TEXT = "新建患者"
NEW_PATIENT_TITLE_TEXTS = ("新建患者", "新建患")
UNSELECTED_PATIENT_TEXT = "未选择患者"
READY_TEXT = "就绪"
PATIENT_ID_TEXT = "患者号"
PATIENT_NAME_TEXT = "姓名"
PATIENT_SEX_TEXT = "性别"
PATIENT_AGE_TEXT = "年龄"
ORDER_DEPARTMENT_TEXT = "开单科室"
REPORT_GENERATED_TEXT = "检查报告已生成"
LINEAR_POLL_SECONDS = 0.5
MSC_EXPLORER_DRIVE_TEXTS = ("RK3588MSC", "RK3568MSC")
MSC_EXPLORER_CONTEXT_TEXTS = ("驱动器工具", "搜索RK3588MSC", "搜索RK3568MSC", "此电脑", "选择要预览的文件")
MSC_EXPLORER_PREVIEW_TEXT = "选择要预览的文件"
MSC_EXPLORER_WAIT_SECONDS = 20.0
MSC_EXPLORER_CLOSE_RETRY_SECONDS = 1.0
MSC_EXPLORER_AFTER_CLOSE_SECONDS = 0.8
MSC_EXPLORER_MAX_CLOSES = 2
BODYPASS_FLOW = "bodypass"
OCT_FLOW = "oct"
PELVIC_FLOOR_FLOW = "pelvic_floor"
ESOPHAGEAL_MOTILITY_FLOW = "esophageal_motility"
BODYPASS_TITLE_TEXTS = ("人体成分数据管理程序", "Body Pass程序")
BODYPASS_TITLE_ASCII_KEYS = ("bodypass", "bodypas")
BODYPASS_MEMBER_ID_TEXT = "编号"
BODYPASS_MEMBER_NAME_TEXT = "姓名"
BODYPASS_RESULT_STATE_TEXT = "显示检测结果"
BODYPASS_TRANSFER_TEXTS = ("传输会员信息", "传输会员")
BODYPASS_DETAIL_TEXTS = ("测量明细",)
BODYPASS_DETAIL_WINDOW_TEXTS = ("检测结果明细",)
BODYPASS_PREVIEW_RESULT_TEXTS = ("预览检测结果",)
BODYPASS_PREVIEW_WINDOW_TEXTS = ("预览", "人体成分分析报告", "模拟签名", "身体成分分析")
BODYPASS_PRINT_TEXTS = ("打印",)
BODYPASS_PRINT_DIALOG_TEXTS = ("打印(P)", "打印（P）", "打印（P)")
BODYPASS_PRINT_DIALOG_READY_TEXTS = ("选择打印机", "打印到文件", "页面范围", "取消") + BODYPASS_PRINT_DIALOG_TEXTS
BODYPASS_CLOSE_TEXTS = ("关闭",)
BODYPASS_TOOLBAR_TRANSFER_OFFSET = (820, 94)
BODYPASS_TOOLBAR_DETAIL_OFFSET = (570, 94)
BODYPASS_PREVIEW_PRINT_OFFSET = (790, 78)
BODYPASS_PREVIEW_CLOSE_OFFSET = (923, 78)
BODYPASS_DETAIL_CLOSE_OFFSET = (920, 190)
BODYPASS_PRINT_DIALOG_PRINT_OFFSET = (388, 512)
BODYPASS_PRINT_DIALOG_FULLSCREEN_PRINT_CENTER = (867, 870)
BODYPASS_MAIN_BOX_FALLBACK = (467, 166, 1479, 895)
BODYPASS_MEMBER_ID_OFFSET = (218, 196)
BODYPASS_MEMBER_NAME_OFFSET = (218, 224)
BODYPASS_FIELD_X_OFFSET = 218
PELVIC_FLOOR_LOGIN_BUTTON = (488, 765)
PELVIC_FLOOR_NEW_PATIENT_BUTTON = (509, 706)
PELVIC_FLOOR_FOREGROUND_NEW_PATIENT_BUTTON = (115, 934)
PELVIC_FLOOR_FINAL_SAVE_BUTTON = (72, 48)
PELVIC_FLOOR_PRINT_REPORT_BUTTON = (1450, 426)
PELVIC_FLOOR_PRINT_CONFIRM_BUTTON = (791, 531)
ESOPHAGEAL_MAIN_BOX_FALLBACK = (17, 8, 1041, 819)
ESOPHAGEAL_TITLE_TEXTS = ("\u98df\u9053\u52a8\u529b\u91c7\u96c6\u7cfb\u7edf", "\u98df\u9053\u52a8\u529b\u91c7\u96c6")
ESOPHAGEAL_FOREGROUND_TEXTS = ("Start", "End", "Std Esoph Motility", "Probe", "Calibration")
ESOPHAGEAL_PATIENT_FORM_TEXTS = (
    "\u60a3\u8005\u4fe1\u606f",
    "\u60a3\u8005 ID",
    "\u60a3\u8005ID",
    "\u6027\u522b",
    "\u751f\u65e5/\u5e74\u9f84",
    "Middle Initial",
    "ID",
)
ESOPHAGEAL_SELECT_BUTTON_OFFSET = (378, 596)
ESOPHAGEAL_INITIAL_CONFIRM_BUTTON_OFFSET = (479, 464)
ESOPHAGEAL_NEW_PATIENT_BUTTON_OFFSET = (45, 76)
ESOPHAGEAL_RIGHT_PRIMARY_BUTTON_OFFSET = (985, 224)
ESOPHAGEAL_STATUS_ROI_OFFSET = (618, 110, 1019, 175)
ESOPHAGEAL_STATUS_TEXT_CENTER_OFFSET = (701, 121)
ESOPHAGEAL_STORE_CLOSE_BUTTON_OFFSET = (330, 477)
ESOPHAGEAL_SAVE_DIALOG_BUTTON_OFFSET = (790, 559)
ESOPHAGEAL_STATUS_POLL_SECONDS = 0.5
ESOPHAGEAL_NEXT_TO_START_SECONDS = 0.7
ESOPHAGEAL_END_CLICK_GAP_SECONDS = 0.75
SEGMENTED_BUTTON_TEXTS = (NEW_PATIENT_TEXT, START_CHECK_TEXT, ANALYSIS_TEXT)
CHECK_COMPLETE_GREEN_MIN_RATIO = 0.28
CHECK_COMPLETE_GREEN_MIN_ROWS = 6
REPORT_DONE_CONFIRM_RATIO = (0.56, 0.565)


def capture_frame_pattern(output: Path) -> Path:
    return output.with_name(f".{output.stem}_%02d{output.suffix}")


def build_capture_command(
    device: str,
    output: Path,
    *,
    width: int,
    height: int,
    framerate: int,
    frames: int,
    io_mode: int,
    capture_format: str,
) -> list[str]:
    fmt = capture_format.lower()
    cmd = [
        "gst-launch-1.0",
        "-q",
        "-e",
        "v4l2src",
        f"device={device}",
    ]
    if io_mode > 0:
        cmd.append(f"io-mode={io_mode}")
    cmd.extend([f"num-buffers={frames}", "!"])
    if fmt in {"mjpg", "mjpeg", "jpeg"}:
        cmd.extend(
            [
                f"image/jpeg,width={width},height={height},framerate={framerate}/1",
                "!",
                "multifilesink",
                f"location={capture_frame_pattern(output).as_posix()}",
            ]
        )
    elif fmt in {"yuyv", "yuy2"}:
        cmd.extend(
            [
                f"video/x-raw,format=YUY2,width={width},height={height},framerate={framerate}/1",
                "!",
                "videoconvert",
                "!",
                "jpegenc",
                "quality=90",
                "!",
                "multifilesink",
                f"location={capture_frame_pattern(output).as_posix()}",
            ]
        )
    elif fmt == "bgr":
        cmd.extend(
            [
                f"video/x-raw,format=BGR,width={width},height={height},framerate={framerate}/1",
                "!",
                "videoconvert",
                "!",
                "jpegenc",
                "quality=90",
                "!",
                "multifilesink",
                f"location={capture_frame_pattern(output).as_posix()}",
            ]
        )
    else:
        raise ValueError(f"unsupported vision capture_format: {capture_format}")
    return cmd


def select_capture_frame(output: Path, frames: int) -> Path | None:
    frames_found = sorted(output.parent.glob(f".{output.stem}_*{output.suffix}"))
    if not frames_found:
        return None
    sizes = []
    for frame in frames_found:
        try:
            size = frame.stat().st_size
        except FileNotFoundError:
            continue
        if size > 0 and is_jpeg_file(frame):
            sizes.append((size, frame.name, frame))
    if not sizes:
        return None
    return max(sizes)[2]


def is_jpeg_file(path: Path) -> bool:
    try:
        with path.open("rb") as handle:
            head = handle.read(2)
            if head != b"\xff\xd8":
                return False
            if handle.seekable():
                handle.seek(-2, 2)
                return handle.read(2) == b"\xff\xd9"
            return True
    except (FileNotFoundError, OSError):
        return False


def capture_frame_size(frame: Path | None) -> int:
    if frame is None:
        return 0
    try:
        return frame.stat().st_size
    except FileNotFoundError:
        return 0


def capture_jpeg(
    device: str,
    output: Path,
    timeout: float,
    *,
    width: int = 1920,
    height: int = 1080,
    framerate: int = 60,
    frames: int = 1,
    io_mode: int = 0,
    capture_format: str = "bgr",
    min_frame_bytes: int = MIN_CAPTURE_FRAME_BYTES,
    max_attempts: int = MAX_CAPTURE_ATTEMPTS,
    retry_delay: float = CAPTURE_RETRY_DELAY_SECONDS,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    cmd = build_capture_command(
        device,
        output,
        width=width,
        height=height,
        framerate=framerate,
        frames=frames,
        io_mode=io_mode,
        capture_format=capture_format,
    )
    selected = None
    for attempt in range(max(1, max_attempts)):
        for frame in output.parent.glob(f".{output.stem}_*{output.suffix}"):
            unlink_missing_ok(frame)
        unlink_missing_ok(output)
        subprocess.run(cmd, check=True, timeout=timeout)
        selected = select_capture_frame(output, frames)
        if capture_frame_size(selected) >= min_frame_bytes:
            break
        if attempt + 1 < max(1, max_attempts):
            time.sleep(retry_delay)
    if selected is not None:
        selected.replace(output)
    for frame in output.parent.glob(f".{output.stem}_*{output.suffix}"):
        unlink_missing_ok(frame)
    if not output.exists() or output.stat().st_size == 0:
        raise RuntimeError(f"capture failed or empty image: {output}")


def capture_jpeg_from_endpoint(
    endpoint: str,
    output: Path,
    timeout: float,
    *,
    min_frame_bytes: int = MIN_CAPTURE_FRAME_BYTES,
) -> None:
    output.parent.mkdir(parents=True, exist_ok=True)
    tmp = output.with_name(f".{output.stem}_endpoint{output.suffix}")
    unlink_missing_ok(tmp)
    unlink_missing_ok(output)
    request = urllib.request.Request(endpoint, headers={"Cache-Control": "no-cache"})
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            if getattr(response, "status", 200) != 200:
                raise RuntimeError(f"{endpoint} HTTP {getattr(response, 'status', 'unknown')}")
            data = response.read()
    except urllib.error.HTTPError as exc:
        error_body = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"{endpoint} HTTP {exc.code}: {error_body}") from exc
    tmp.write_bytes(data)
    frame_size = capture_frame_size(tmp)
    if frame_size < min_frame_bytes or not is_jpeg_file(tmp):
        unlink_missing_ok(tmp)
        raise RuntimeError(f"invalid endpoint frame from {endpoint}: {frame_size} bytes")
    tmp.replace(output)


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
    request = urllib.request.Request(
        endpoint,
        data=build_image_body(image_base64, extra),
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


def ocr_endpoint_from_window_endpoint(endpoint: str) -> str:
    text = str(endpoint).rstrip("/")
    if text.endswith("/window/detect"):
        return text[: -len("/window/detect")] + "/ocr"
    if text.endswith("/detect_window"):
        return text[: -len("/detect_window")] + "/ocr"
    if text.endswith("/ocr"):
        return text
    return text + "/ocr"


def crop_jpeg_region(source: Path, output: Path, box: tuple[int, int, int, int]) -> None:
    try:
        from PIL import Image
    except ImportError as exc:
        raise RuntimeError("Pillow is required for ROI OCR cropping") from exc

    with Image.open(source) as image:
        width, height = image.size
        left = max(0, min(width - 1, int(box[0])))
        top = max(0, min(height - 1, int(box[1])))
        right = max(left + 1, min(width, int(box[2])))
        bottom = max(top + 1, min(height, int(box[3])))
        image.crop((left, top, right, bottom)).save(output, "JPEG", quality=90)


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


def extract_box(response: dict[str, Any]) -> tuple[int, int, int, int] | None:
    box = response.get("box")
    if not isinstance(box, list) or len(box) != 4:
        return None
    if not all(isinstance(value, (int, float)) for value in box):
        return None
    x1, y1, x2, y2 = box
    return int(round(x1)), int(round(y1)), int(round(x2)), int(round(y2))


def response_windows(response: dict[str, Any]) -> list[dict[str, Any]]:
    windows = response.get("windows")
    if isinstance(windows, list):
        parsed = [item for item in windows if isinstance(item, dict)]
        if parsed:
            return parsed
    return [response]


def find_ocr_center(response: dict[str, Any], text: str) -> tuple[int, int] | None:
    for item in ocr_items(response):
        if str(item.get("text", "")).strip() != text:
            continue
        center = extract_center(item, "center")
        if center is not None:
            return center
    return None


def ocr_items(response: dict[str, Any]) -> list[dict[str, Any]]:
    items = []
    for window in response_windows(response):
        ocr = window.get("ocr")
        if not isinstance(ocr, list):
            continue
        for item in ocr:
            if isinstance(item, dict):
                items.append(item)
    return items


def ocr_texts(response: dict[str, Any]) -> list[str]:
    texts = []
    for item in ocr_items(response):
        texts.append(str(item.get("text", "")).strip())
    return texts


def compact_ocr_text(text: str) -> str:
    return "".join(str(text).split())


def ocr_contains(response: dict[str, Any], text: str) -> bool:
    compact_text = compact_ocr_text(text)
    return any(text in item or compact_text in compact_ocr_text(item) for item in ocr_texts(response))


def find_ocr_item_containing(response: dict[str, Any], *texts: str) -> dict[str, Any] | None:
    compact_texts = [compact_ocr_text(text) for text in texts if text]
    for item in ocr_items(response):
        raw = str(item.get("text", "")).strip()
        compact = compact_ocr_text(raw)
        if any(text in raw or compact_text in compact for text, compact_text in zip(texts, compact_texts)):
            return item
    return None


def find_ocr_center_containing(response: dict[str, Any], *texts: str) -> tuple[int, int] | None:
    item = find_ocr_item_containing(response, *texts)
    if item is None:
        return None
    return extract_center(item, "center")


def find_action_center(response: dict[str, Any], text: str) -> tuple[int, int] | None:
    center = find_ocr_center(response, text)
    if center is not None:
        return center
    if text not in SEGMENTED_BUTTON_TEXTS:
        return None

    target = compact_ocr_text(text)
    if not target:
        return None
    for item in ocr_items(response):
        raw = str(item.get("text", "")).strip()
        compact = compact_ocr_text(raw)
        start = compact.find(target)
        if start < 0:
            continue
        box = extract_box(item)
        if box is None:
            return extract_center(item, "center")
        x1, y1, x2, y2 = box
        x = x1 + (x2 - x1) * ((start + len(target) / 2) / max(len(compact), 1))
        y = (y1 + y2) / 2
        return int(round(x)), int(round(y))
    return None


def first_window_box(response: dict[str, Any]) -> tuple[int, int, int, int] | None:
    for window in response_windows(response):
        box = extract_box(window)
        if box is not None:
            return box
    return extract_box(response)


def bodypass_main_window(response: dict[str, Any]) -> dict[str, Any] | None:
    for window in response_windows(response):
        if is_bodypass_title_window(window):
            return window
    if is_bodypass_title_window(response):
        return response
    return None


def is_bodypass_title_window(response: dict[str, Any]) -> bool:
    if any(ocr_contains(response, text) for text in BODYPASS_TITLE_TEXTS):
        return True

    box = extract_box(response)
    if box is not None:
        top_limit = box[1] + max(int((box[3] - box[1]) * 0.35), 80)
    else:
        top_limit = 380

    for item in ocr_items(response):
        text = compact_ocr_text(str(item.get("text", ""))).lower()
        if not text:
            continue
        if not any(key in text for key in BODYPASS_TITLE_ASCII_KEYS):
            continue
        center = extract_center(item, "center")
        if center is not None and center[1] <= top_limit:
            return True
    return False


def is_bodypass_main_window(response: dict[str, Any]) -> bool:
    return bodypass_main_window(response) is not None


def bodypass_window_box(response: dict[str, Any]) -> tuple[int, int, int, int] | None:
    window = bodypass_main_window(response)
    if window is not None:
        return resolve_bodypass_main_box(extract_box(window))
    return resolve_bodypass_main_box(first_window_box(response))


def resolve_bodypass_main_box(box: tuple[int, int, int, int] | None) -> tuple[int, int, int, int]:
    if box is None:
        return BODYPASS_MAIN_BOX_FALLBACK
    x1, y1, x2, y2 = box
    if x1 <= 10 and y1 <= 10 and x2 >= 1910 and y2 >= 1070:
        return BODYPASS_MAIN_BOX_FALLBACK
    return box


def is_fullscreen_box(box: tuple[int, int, int, int] | None) -> bool:
    if box is None:
        return False
    x1, y1, x2, y2 = box
    return x1 <= 10 and y1 <= 10 and x2 >= 1910 and y2 >= 1070


def esophageal_main_window(response: dict[str, Any]) -> dict[str, Any] | None:
    for window in response_windows(response):
        if any(ocr_contains(window, text) for text in ESOPHAGEAL_TITLE_TEXTS):
            return window
    if any(ocr_contains(response, text) for text in ESOPHAGEAL_TITLE_TEXTS):
        return response
    return None


def is_esophageal_main_window(response: dict[str, Any]) -> bool:
    return esophageal_main_window(response) is not None


def is_esophageal_foreground(response: dict[str, Any]) -> bool:
    return is_esophageal_main_window(response) or any(ocr_contains(response, text) for text in ESOPHAGEAL_FOREGROUND_TEXTS)


def normalize_esophageal_main_box(box: tuple[int, int, int, int] | None) -> tuple[int, int, int, int]:
    if box is None:
        return ESOPHAGEAL_MAIN_BOX_FALLBACK
    x1, y1, x2, y2 = box
    width = x2 - x1
    height = y2 - y1
    if width < 600 or height < 450 or width > 1500 or height > 1000:
        return ESOPHAGEAL_MAIN_BOX_FALLBACK
    return box


def esophageal_anchor_from_status_text(response: dict[str, Any]) -> tuple[int, int, int, int] | None:
    for item in ocr_items(response):
        raw = str(item.get("text", "")).strip()
        if "Start" not in raw and "End" not in raw:
            continue
        center = extract_center(item, "center")
        if center is None:
            continue
        left = center[0] - ESOPHAGEAL_STATUS_TEXT_CENTER_OFFSET[0]
        top = center[1] - ESOPHAGEAL_STATUS_TEXT_CENTER_OFFSET[1]
        if left < -50 or top < -50 or left > 500 or top > 300:
            continue
        width = ESOPHAGEAL_MAIN_BOX_FALLBACK[2] - ESOPHAGEAL_MAIN_BOX_FALLBACK[0]
        height = ESOPHAGEAL_MAIN_BOX_FALLBACK[3] - ESOPHAGEAL_MAIN_BOX_FALLBACK[1]
        return (left, top, left + width, top + height)
    return None


def esophageal_window_box(response: dict[str, Any]) -> tuple[int, int, int, int]:
    inferred = esophageal_anchor_from_status_text(response)
    if inferred is not None:
        return inferred
    window = esophageal_main_window(response)
    box = extract_box(window) if window is not None else first_window_box(response)
    return normalize_esophageal_main_box(box)


def esophageal_end_prompt_ready(response: dict[str, Any]) -> bool:
    return ocr_contains(response, "End") and (
        ocr_contains(response, "\u7ed3\u675f\u68c0\u6d4b")
        or ocr_contains(response, "\u65b0\u60a3\u8005")
        or ocr_contains(response, "\u7ed3\u675f")
    )


def is_esophageal_patient_form(response: dict[str, Any]) -> bool:
    return sum(1 for text in ESOPHAGEAL_PATIENT_FORM_TEXTS if ocr_contains(response, text)) >= 2


def bodypass_form_label_item(window: dict[str, Any], label: str) -> dict[str, Any] | None:
    box = extract_box(window)
    if box is None:
        box = (0, 0, 1920, 1080)
    x1, y1, x2, y2 = box
    width = max(x2 - x1, 1)
    height = max(y2 - y1, 1)
    candidates: list[tuple[int, int, dict[str, Any]]] = []
    for item in ocr_items(window):
        text = str(item.get("text", "")).strip()
        if compact_ocr_text(text) != compact_ocr_text(label):
            continue
        center = extract_center(item, "center")
        if center is None:
            continue
        cx, cy = center
        if not (x1 <= cx <= x1 + int(width * 0.38)):
            continue
        if not (y1 + int(height * 0.18) <= cy <= y1 + int(height * 0.42)):
            continue
        candidates.append((cy, cx, item))
    if not candidates:
        return None
    return sorted(candidates)[0][2]


def bodypass_input_center(window: dict[str, Any], label: str) -> tuple[int, int] | None:
    item = bodypass_form_label_item(window, label)
    if item is None:
        return None
    center = extract_center(item, "center")
    if center is None:
        return None
    box = extract_box(window)
    label_box = extract_box(item)
    if box is not None:
        x = box[0] + BODYPASS_FIELD_X_OFFSET
    elif label_box is not None:
        x = label_box[2] + 160
    else:
        x = center[0] + 180
    return int(x), int(center[1])


def bodypass_fixed_input_center(window: dict[str, Any], offset: tuple[int, int]) -> tuple[int, int]:
    box = resolve_bodypass_main_box(extract_box(window))
    return int(box[0] + offset[0]), int(box[1] + offset[1])


def bodypass_machine_state_ready(response: dict[str, Any]) -> bool:
    for text in ocr_texts(response):
        compact = compact_ocr_text(text)
        if BODYPASS_RESULT_STATE_TEXT in text:
            return True
        if "MachineState" in compact and compact_ocr_text(BODYPASS_RESULT_STATE_TEXT) in compact:
            return True
    return False


def bodypass_contains_any(response: dict[str, Any], texts: tuple[str, ...]) -> bool:
    return any(ocr_contains(response, text) for text in texts)


def bodypass_print_dialog_window(response: dict[str, Any]) -> dict[str, Any] | None:
    for window in response_windows(response):
        if bodypass_contains_any(window, BODYPASS_PRINT_DIALOG_READY_TEXTS):
            return window
    if bodypass_contains_any(response, BODYPASS_PRINT_DIALOG_READY_TEXTS):
        return response
    return None


def is_bodypass_print_dialog(response: dict[str, Any]) -> bool:
    return bodypass_print_dialog_window(response) is not None


def msc_explorer_close_center(response: dict[str, Any]) -> tuple[int, int] | None:
    has_drive = any(
        any(drive_text in compact_ocr_text(text) for drive_text in MSC_EXPLORER_DRIVE_TEXTS)
        for text in ocr_texts(response)
    )
    has_context = any(ocr_contains(response, text) for text in MSC_EXPLORER_CONTEXT_TEXTS)
    if not has_drive or not has_context:
        return None

    image_size = response.get("image_size") if isinstance(response.get("image_size"), dict) else {}
    image_width = int(image_size.get("width") or 1920)
    image_height = int(image_size.get("height") or 1080)
    title_center = None
    title_y = None
    preview_box = None
    max_keyword_right = None

    for item in ocr_items(response):
        text = str(item.get("text", "")).strip()
        compact = compact_ocr_text(text)
        center = extract_center(item, "center")
        box = extract_box(item)
        is_drive_text = any(drive_text in compact for drive_text in MSC_EXPLORER_DRIVE_TEXTS)
        is_context_text = any(compact_ocr_text(keyword) in compact for keyword in MSC_EXPLORER_CONTEXT_TEXTS)
        if is_drive_text and center is not None and center[1] < image_height * 0.45:
            if title_y is None or center[1] < title_y:
                title_center = center
                title_y = center[1]
        if MSC_EXPLORER_PREVIEW_TEXT in text and box is not None:
            preview_box = box
        if (is_drive_text or is_context_text) and box is not None:
            max_keyword_right = box[2] if max_keyword_right is None else max(max_keyword_right, box[2])

    if title_center is None:
        return None

    if preview_box is not None:
        close_x = preview_box[2] + 72
    elif max_keyword_right is not None:
        close_x = max(max_keyword_right + 72, title_center[0] + int(image_width * 0.33))
    else:
        close_x = title_center[0] + int(image_width * 0.33)
    close_y = title_center[1]
    close_x = max(8, min(image_width - 8, close_x))
    close_y = max(8, min(image_height - 8, close_y))
    return int(close_x), int(close_y)


def is_loading(response: dict[str, Any]) -> bool:
    loading_words = ("加载", "正在加载", "请稍候", "请稍等", "处理中", "等待")
    return any(any(word in item for word in loading_words) for item in ocr_texts(response))


def label_value(response: dict[str, Any]) -> str | None:
    label = response.get("label")
    if label is None:
        return None
    label = str(label).strip()
    if not label or label.lower() == "none":
        return None
    return label


def labeled_windows(response: dict[str, Any], *labels: str) -> list[dict[str, Any]]:
    wanted = set(labels)
    return [window for window in response_windows(response) if label_value(window) in wanted]


def has_label(response: dict[str, Any], label: str) -> bool:
    return bool(labeled_windows(response, label))


def has_no_detected_window(response: dict[str, Any]) -> bool:
    return not any(label_value(window) for window in response_windows(response))


def is_login_window(response: dict[str, Any]) -> bool:
    if ocr_contains(response, LOGIN_TITLE_TEXT):
        return True
    return (
        find_ocr_center(response, LOGIN_TEXT) is not None
        and (
            ocr_contains(response, LOGIN_TITLE_TEXT)
            or (ocr_contains(response, USERNAME_TEXT) and ocr_contains(response, PASSWORD_TEXT))
        )
    )


def login_button_center(response: dict[str, Any]) -> tuple[int, int] | None:
    center = find_ocr_center(response, LOGIN_TEXT)
    if center is not None:
        return center
    title_center = find_ocr_center(response, LOGIN_TITLE_TEXT)
    if title_center is None:
        return None
    return (
        title_center[0] + LOGIN_BUTTON_TITLE_OFFSET[0],
        title_center[1] + LOGIN_BUTTON_TITLE_OFFSET[1],
    )


def has_new_patient_title(response: dict[str, Any]) -> bool:
    return any(ocr_contains(response, text) for text in NEW_PATIENT_TITLE_TEXTS)


def is_new_patient_window(response: dict[str, Any]) -> bool:
    return (
        has_label(response, "2")
        or (
            ocr_contains(response, ORDER_DEPARTMENT_TEXT)
            and ocr_contains(response, PATIENT_ID_TEXT)
            and confirm_button_target(response) is not None
        )
        or (
            has_new_patient_title(response)
            and ocr_contains(response, PATIENT_ID_TEXT)
        )
        or (
            ocr_contains(response, NEW_PATIENT_TEXT)
            and ocr_contains(response, PATIENT_ID_TEXT)
            and ocr_contains(response, PATIENT_NAME_TEXT)
            and (ocr_contains(response, PATIENT_SEX_TEXT) or ocr_contains(response, PATIENT_AGE_TEXT))
            and confirm_button_target(response) is not None
        )
    )


def is_ready_to_create_patient(response: dict[str, Any]) -> bool:
    return (
        find_action_center(response, NEW_PATIENT_TEXT) is not None
        and (
            ocr_contains(response, UNSELECTED_PATIENT_TEXT)
            or ocr_contains(response, READY_TEXT)
            or ocr_contains(response, CHECK_DONE_TEXT)
            or find_action_center(response, START_CHECK_TEXT) is not None
        )
    )


def is_ready_to_start_check(response: dict[str, Any]) -> bool:
    return (
        find_action_center(response, START_CHECK_TEXT) is not None
        and not ocr_contains(response, UNSELECTED_PATIENT_TEXT)
    )


def is_check_complete(response: dict[str, Any]) -> bool:
    return (
        (
            ocr_contains(response, CHECK_DONE_TEXT)
            or is_check_complete_progress_bar(response.get("_image_path"))
        )
        and find_action_center(response, ANALYSIS_TEXT) is not None
    )


def is_check_complete_progress_bar(image_path: Any) -> bool:
    if not image_path:
        return False
    try:
        from PIL import Image

        with Image.open(Path(str(image_path))) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            left = 0
            right = int(width * 0.55)
            top = int(height * 0.52)
            bottom = int(height * 0.78)
            crop = rgb.crop((left, top, right, bottom))
            pixels = crop.load()
            required_run = max(220, int(width * CHECK_COMPLETE_GREEN_MIN_RATIO))
            matched_rows = 0

            for y in range(crop.height):
                run = 0
                row_max = 0
                for x in range(crop.width):
                    r, g, b = pixels[x, y]
                    if g >= 110 and r <= 90 and b <= 120 and g >= r * 1.5 and g >= b * 1.2:
                        run += 1
                        row_max = max(row_max, run)
                    else:
                        run = 0
                if row_max >= required_run:
                    matched_rows += 1
                    if matched_rows >= CHECK_COMPLETE_GREEN_MIN_ROWS:
                        return True
            return False
    except Exception as exc:
        LOGGER.debug("vision progress bar check failed for %s: %s", image_path, exc)
        return False


def has_pdf_report_prompt_hint(response: dict[str, Any]) -> bool:
    if ocr_contains(response, PDF_REPORT_PROMPT_TEXT):
        return True
    report_text = PDF_REPORT_PROMPT_TEXT[-2:]
    for text in ocr_texts(response):
        compact = compact_ocr_text(text)
        if "PDF" in compact.upper() and report_text in compact:
            return True
    return False


def is_pdf_report_prompt(response: dict[str, Any]) -> bool:
    return has_pdf_report_prompt_hint(response) and pdf_report_yes_target(response) is not None


def confirm_button_target(response: dict[str, Any]) -> str | None:
    return next((text for text in CONFIRM_BUTTON_TEXTS if find_ocr_center(response, text) is not None), None)


def is_report_generated(response: dict[str, Any]) -> bool:
    return (
        ocr_contains(response, REPORT_GENERATED_TEXT)
        and confirm_button_target(response) is not None
    ) or report_generated_visual_confirm_center(response.get("_image_path")) is not None


def count_blue_pixels(image: Any, box: tuple[int, int, int, int]) -> int:
    crop = image.crop(box)
    pixels = crop.load()
    count = 0
    for y in range(crop.height):
        for x in range(crop.width):
            r, g, b = pixels[x, y]
            if b >= 130 and g >= 60 and r <= 120 and b >= g * 1.15 and b >= max(r * 1.8, 1):
                count += 1
    return count


def count_light_neutral_pixels(image: Any, box: tuple[int, int, int, int]) -> int:
    crop = image.crop(box)
    pixels = crop.load()
    count = 0
    for y in range(crop.height):
        for x in range(crop.width):
            r, g, b = pixels[x, y]
            if (
                r >= 205
                and g >= 205
                and b >= 205
                and abs(r - g) <= 18
                and abs(g - b) <= 18
                and abs(r - b) <= 18
            ):
                count += 1
    return count


def report_generated_visual_confirm_center(image_path: Any) -> tuple[int, int] | None:
    if not image_path:
        return None
    try:
        from PIL import Image

        with Image.open(Path(str(image_path))) as image:
            rgb = image.convert("RGB")
            width, height = rgb.size
            dialog_box = (
                int(width * 0.40),
                int(height * 0.42),
                int(width * 0.60),
                int(height * 0.62),
            )
            icon_box = (
                int(width * 0.415),
                int(height * 0.465),
                int(width * 0.445),
                int(height * 0.505),
            )
            button_box = (
                int(width * 0.535),
                int(height * 0.55),
                int(width * 0.585),
                int(height * 0.58),
            )
            dialog_area = max((dialog_box[2] - dialog_box[0]) * (dialog_box[3] - dialog_box[1]), 1)
            if count_light_neutral_pixels(rgb, dialog_box) < int(dialog_area * 0.30):
                return None
            if count_blue_pixels(rgb, icon_box) < 40:
                return None
            if count_blue_pixels(rgb, button_box) < 35:
                return None
            return (
                int(width * REPORT_DONE_CONFIRM_RATIO[0]),
                int(height * REPORT_DONE_CONFIRM_RATIO[1]),
            )
    except Exception as exc:
        LOGGER.debug("vision report generated visual check failed for %s: %s", image_path, exc)
        return None


def pdf_report_yes_target(response: dict[str, Any]) -> str | None:
    if not has_pdf_report_prompt_hint(response):
        return None
    best_text = None
    best_y = None
    for item in ocr_items(response):
        text = str(item.get("text", "")).strip()
        center = extract_center(item, "center")
        if "是" not in text or center is None:
            continue
        if best_y is None or center[1] > best_y:
            best_text = text
            best_y = center[1]
    return best_text


def dialog_button_target(response: dict[str, Any], *labels: str) -> str | None:
    for window in labeled_windows(response, *labels):
        if label_value(window) == "4":
            pdf_target = pdf_report_yes_target(window)
            if pdf_target:
                return pdf_target
        for text in YES_BUTTON_TEXTS + CONFIRM_BUTTON_TEXTS:
            if find_ocr_center(window, text) is not None:
                return text
    return None


def decide_action(response: dict[str, Any]) -> tuple[str, str | None]:
    if is_pdf_report_prompt(response):
        target = pdf_report_yes_target(response)
        if target:
            return "click_text", target
        return "wait", None
    if is_report_generated(response):
        return "click_text", confirm_button_target(response) or "确定"
    legacy_dialog_target = dialog_button_target(response, "4", "5")
    if legacy_dialog_target:
        return "click_text", legacy_dialog_target
    if labeled_windows(response, "4", "5"):
        return "wait", None
    if is_new_patient_window(response):
        return "form_input", None
    if is_login_window(response):
        return "click_text", LOGIN_TEXT

    if ocr_contains(response, UNSELECTED_PATIENT_TEXT):
        return "click_text", NEW_PATIENT_TEXT
    if ocr_contains(response, CHECK_DONE_TEXT) and find_action_center(response, ANALYSIS_TEXT) is not None:
        return "analysis", None
    if find_action_center(response, START_CHECK_TEXT) is not None:
        return "click_text", START_CHECK_TEXT

    if not any(label_value(window) for window in response_windows(response)):
        if is_loading(response):
            return "wait", None
        return "open", None

    for window in labeled_windows(response, "1"):
        if ocr_contains(window, "就绪"):
            return "wait", None
        return "wait", None
    return "wait", None


def decide_after_analysis(response: dict[str, Any]) -> tuple[str, str | None]:
    if is_pdf_report_prompt(response):
        target = pdf_report_yes_target(response)
        if target:
            return "click_text", target
        return "wait", None
    if is_report_generated(response):
        return "click_text", confirm_button_target(response) or "确定"
    legacy_dialog_target = dialog_button_target(response, "4", "5")
    if legacy_dialog_target:
        return "click_text", legacy_dialog_target
    if labeled_windows(response, "4", "5"):
        return "wait", None
    if ocr_contains(response, CHECK_DONE_TEXT):
        return "finish", None
    return "wait", None


class VisionFlow:
    def __init__(self, config: Any, hid_output: Any) -> None:
        self.config = config
        self.hid_output = hid_output
        self.workdir = Path(config.workdir)
        self.capture_index = 0
        self.bodypass_main_box: tuple[int, int, int, int] | None = None
        self.esophageal_main_box: tuple[int, int, int, int] | None = None

    async def run_until_form_done(self, task: dict[str, Any], on_hid_start: Callable[[], None] | None = None) -> str:
        if not self.config.enabled:
            self._notify_hid_start(on_hid_start)
            await self.hid_output.execute_form(task)
            return "form_done"
        flow = str(getattr(self.config, "flow", "body_composition"))
        if flow == BODYPASS_FLOW:
            return await self.run_bodypass_until_done(task, on_hid_start=on_hid_start)
        if flow == OCT_FLOW:
            return await self.run_oct_until_form_done(task, on_hid_start=on_hid_start)
        if flow == PELVIC_FLOOR_FLOW:
            return await self.run_pelvic_floor_until_done(task, on_hid_start=on_hid_start)
        if flow == ESOPHAGEAL_MOTILITY_FLOW:
            return await self.run_esophageal_motility_until_done(task, on_hid_start=on_hid_start)

        started_at = time.monotonic()
        await self.prepare_new_patient_window(started_at)
        LOGGER.info("vision label=2; start hid form input")
        self._notify_hid_start(on_hid_start)
        await self.hid_output.execute_form(task)
        await self.sleep(self.config.wait_after_action)

        await self.wait_and_click_start_check(started_at)
        await self.wait_and_click_data_analysis(started_at)
        await self.wait_and_click_pdf_yes(started_at)
        await self.wait_and_confirm_report_generated(started_at)
        await self.wait_and_click_new_patient_to_finish(started_at)
        return "analysis_finished"

    def _notify_hid_start(self, callback: Callable[[], None] | None) -> None:
        if callback is None:
            return
        try:
            callback()
        except Exception:
            LOGGER.exception("vision hid start callback failed")

    def check_runtime(self, started_at: float) -> None:
        if self.config.max_runtime > 0 and time.monotonic() - started_at > self.config.max_runtime:
            raise asyncio.TimeoutError(f"vision flow max runtime reached: {self.config.max_runtime:.1f}s")

    def capture_options(self) -> dict[str, Any]:
        return {
            "width": int(getattr(self.config, "capture_width", 1920)),
            "height": int(getattr(self.config, "capture_height", 1080)),
            "framerate": int(getattr(self.config, "capture_framerate", 60)),
            "frames": int(getattr(self.config, "capture_frames", 1)),
            "io_mode": int(getattr(self.config, "capture_io_mode", 0)),
            "capture_format": str(getattr(self.config, "capture_format", "bgr")),
        }

    async def capture_screen(self, image_path: Path) -> None:
        frame_endpoint = str(getattr(self.config, "frame_endpoint", "") or "").strip()
        if frame_endpoint:
            try:
                await to_thread(
                    capture_jpeg_from_endpoint,
                    frame_endpoint,
                    image_path,
                    self.config.timeout_seconds,
                )
                return
            except Exception as exc:
                LOGGER.warning("vision frame endpoint failed, fallback to local capture: %s", exc)
        await to_thread(capture_jpeg, self.config.device, image_path, self.config.timeout_seconds, **self.capture_options())

    async def run_oct_until_form_done(self, task: dict[str, Any], on_hid_start: Callable[[], None] | None = None) -> str:
        ok = await self.open_app(f"oct_open_{self.capture_index + 1}.jpg")
        if ok:
            LOGGER.info("vision oct icon opened; wait %.1fs before hid form input", self.config.wait_after_open)
            await self.sleep(self.config.wait_after_open)
        else:
            LOGGER.info("vision oct icon not found; assume OCT is already foreground and start hid form input")
        self._notify_hid_start(on_hid_start)
        await self.hid_output.execute_form(task)
        return "form_done"

    async def run_pelvic_floor_until_done(
        self,
        task: dict[str, Any],
        on_hid_start: Callable[[], None] | None = None,
    ) -> str:
        ok = await self.open_app(f"pelvic_floor_open_{self.capture_index + 1}.jpg")
        if ok:
            LOGGER.info("vision pelvic_floor icon opened; wait %.1fs before login", self.config.wait_after_open)
            await self.sleep(self.config.wait_after_open)
            await self.click_fixed(PELVIC_FLOOR_LOGIN_BUTTON, "pelvic_floor_login")
            await self.sleep(self.config.wait_after_action)
            await self.click_fixed(PELVIC_FLOOR_NEW_PATIENT_BUTTON, "pelvic_floor_new_patient")
            await self.sleep(self.config.wait_after_action)
        else:
            LOGGER.info("vision pelvic_floor icon not found; assume app is already foreground")
            await self.click_fixed(PELVIC_FLOOR_FOREGROUND_NEW_PATIENT_BUTTON, "pelvic_floor_foreground_new_patient")
            await self.sleep(self.config.wait_after_action)

        self._notify_hid_start(on_hid_start)
        await self.hid_output.execute_form(task)
        await self.sleep(self.config.wait_after_action)
        await self.click_fixed(PELVIC_FLOOR_FINAL_SAVE_BUTTON, "pelvic_floor_final_save")
        await self.sleep(self.config.wait_after_action)
        await self.click_fixed(PELVIC_FLOOR_PRINT_REPORT_BUTTON, "pelvic_floor_print_report")
        await self.sleep(self.config.wait_after_action)
        await self.click_fixed(PELVIC_FLOOR_PRINT_CONFIRM_BUTTON, "pelvic_floor_print_confirm")
        return "pelvic_floor_finished"

    async def run_esophageal_motility_until_done(
        self,
        task: dict[str, Any],
        on_hid_start: Callable[[], None] | None = None,
    ) -> str:
        started_at = time.monotonic()
        patient_form_ready = await self.prepare_esophageal_main_window(started_at)

        if not patient_form_ready:
            await self.click_esophageal_relative(ESOPHAGEAL_NEW_PATIENT_BUTTON_OFFSET, "esophageal_new_patient")
            await self.sleep(self.config.wait_after_action)
        else:
            LOGGER.info("vision esophageal patient form already open; skip new patient click")

        self._notify_hid_start(on_hid_start)
        await self.hid_output.execute_form(self.esophageal_form_task(task))
        await self.sleep(self.config.wait_after_action)

        await self.click_esophageal_relative(ESOPHAGEAL_RIGHT_PRIMARY_BUTTON_OFFSET, "esophageal_next")
        await self.sleep(ESOPHAGEAL_NEXT_TO_START_SECONDS)
        await self.click_esophageal_relative(ESOPHAGEAL_RIGHT_PRIMARY_BUTTON_OFFSET, "esophageal_start")
        await self.wait_for_esophageal_end_prompt(started_at)
        await self.click_esophageal_relative(ESOPHAGEAL_RIGHT_PRIMARY_BUTTON_OFFSET, "esophageal_end_1")
        await self.sleep(ESOPHAGEAL_END_CLICK_GAP_SECONDS)
        await self.click_esophageal_relative(ESOPHAGEAL_RIGHT_PRIMARY_BUTTON_OFFSET, "esophageal_end_2")
        await self.sleep(self.config.wait_after_action)
        await self.click_esophageal_relative(ESOPHAGEAL_STORE_CLOSE_BUTTON_OFFSET, "esophageal_store_close")
        await self.sleep(self.config.wait_after_action)
        await self.click_esophageal_relative(ESOPHAGEAL_SAVE_DIALOG_BUTTON_OFFSET, "esophageal_save_dialog")
        await self.sleep(self.config.wait_after_action)
        await self.click_esophageal_relative(ESOPHAGEAL_NEW_PATIENT_BUTTON_OFFSET, "esophageal_final_new_patient")
        return "esophageal_motility_finished"

    async def prepare_esophageal_main_window(self, started_at: float) -> bool:
        response = await self.detect_window(f"esophageal_probe_{self.capture_index + 1}.jpg")
        if is_esophageal_foreground(response):
            self.esophageal_main_box = esophageal_window_box(response)
            LOGGER.info("vision esophageal foreground detected box=%s", self.esophageal_main_box)
            return is_esophageal_patient_form(response)

        self.esophageal_main_box = ESOPHAGEAL_MAIN_BOX_FALLBACK
        ok = await self.open_app(f"esophageal_open_{self.capture_index + 1}.jpg")
        if not ok:
            raise RuntimeError("vision esophageal icon not found and main window not detected")
        LOGGER.info("vision esophageal icon opened; wait %.1fs before setup", self.config.wait_after_open)
        await self.sleep(self.config.wait_after_open)

        self.check_runtime(started_at)
        response = await self.detect_window(f"esophageal_after_open_{self.capture_index + 1}.jpg")
        if is_esophageal_main_window(response):
            self.esophageal_main_box = esophageal_window_box(response)
            LOGGER.info("vision esophageal main window detected after open box=%s", self.esophageal_main_box)

        await self.click_esophageal_relative(ESOPHAGEAL_SELECT_BUTTON_OFFSET, "esophageal_select")
        await self.sleep(self.config.wait_after_action)
        await self.click_esophageal_relative(ESOPHAGEAL_INITIAL_CONFIRM_BUTTON_OFFSET, "esophageal_initial_confirm")
        await self.sleep(self.config.wait_after_action)
        return False

    async def wait_for_esophageal_end_prompt(self, started_at: float) -> None:
        while True:
            self.check_runtime(started_at)
            response = await self.detect_ocr_roi(
                f"esophageal_status_{self.capture_index + 1}.jpg",
                self.esophageal_status_roi_box(),
            )
            if esophageal_end_prompt_ready(response):
                LOGGER.info("vision esophageal status ready for End")
                return
            LOGGER.info("vision esophageal status wait texts=%s", ocr_texts(response))
            await self.sleep(ESOPHAGEAL_STATUS_POLL_SECONDS)

    def esophageal_anchor_box(self) -> tuple[int, int, int, int]:
        return self.esophageal_main_box or ESOPHAGEAL_MAIN_BOX_FALLBACK

    def esophageal_abs_point(self, offset: tuple[int, int]) -> tuple[int, int]:
        box = self.esophageal_anchor_box()
        return box[0] + offset[0], box[1] + offset[1]

    def esophageal_abs_box(self, offset: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
        box = self.esophageal_anchor_box()
        return box[0] + offset[0], box[1] + offset[1], box[0] + offset[2], box[1] + offset[3]

    def esophageal_status_roi_box(self) -> tuple[int, int, int, int]:
        return self.esophageal_abs_box(ESOPHAGEAL_STATUS_ROI_OFFSET)

    def esophageal_form_task(self, task: dict[str, Any]) -> dict[str, Any]:
        anchored = dict(task)
        events = []
        for event in task.get("eventClassList", []):
            if not isinstance(event, dict):
                continue
            item = dict(event)
            if "x" in item and "y" in item:
                point = self.esophageal_abs_point((int(item.get("x", 0)), int(item.get("y", 0))))
                item["x"], item["y"] = point
            events.append(item)
        anchored["eventClassList"] = events
        return anchored

    async def click_esophageal_relative(self, offset: tuple[int, int], action: str) -> None:
        center = self.esophageal_abs_point(offset)
        LOGGER.info("vision esophageal action=%s target=%s offset=%s", action, center, offset)
        await self.hid_output.click(center[0], center[1])

    async def run_bodypass_until_done(self, task: dict[str, Any], on_hid_start: Callable[[], None] | None = None) -> str:
        started_at = time.monotonic()
        response = await self.prepare_bodypass_main_window(started_at)
        self._notify_hid_start(on_hid_start)
        await self.input_bodypass_member(response, task)
        await self.sleep(self.config.wait_after_action)

        await self.click_bodypass_toolbar_button(
            response,
            BODYPASS_TRANSFER_TEXTS,
            BODYPASS_TOOLBAR_TRANSFER_OFFSET,
            "bodypass_transfer_member",
        )
        await self.sleep(self.config.wait_after_action)

        response = await self.wait_for_bodypass_condition(
            started_at,
            "bodypass_result_state",
            bodypass_machine_state_ready,
        )
        await self.click_bodypass_toolbar_button(
            response,
            BODYPASS_DETAIL_TEXTS,
            BODYPASS_TOOLBAR_DETAIL_OFFSET,
            "bodypass_measure_detail",
        )
        await self.sleep(self.config.wait_after_action)

        response = await self.wait_for_bodypass_condition(
            started_at,
            "bodypass_detail_window",
            lambda item: bodypass_contains_any(item, BODYPASS_DETAIL_WINDOW_TEXTS)
            or bodypass_contains_any(item, BODYPASS_PREVIEW_RESULT_TEXTS),
        )
        await self.click_ocr_text_any_required(response, BODYPASS_PREVIEW_RESULT_TEXTS)
        await self.sleep(self.config.wait_after_action)

        response = await self.wait_for_bodypass_condition(
            started_at,
            "bodypass_preview_window",
            lambda item: bodypass_contains_any(item, BODYPASS_PREVIEW_WINDOW_TEXTS),
        )
        await self.click_bodypass_toolbar_button(
            response,
            BODYPASS_PRINT_TEXTS,
            BODYPASS_PREVIEW_PRINT_OFFSET,
            "bodypass_preview_print",
        )
        await self.sleep(self.config.wait_after_action)

        response = await self.wait_for_bodypass_condition(
            started_at,
            "bodypass_print_dialog",
            is_bodypass_print_dialog,
        )
        await self.click_bodypass_print_dialog(response)
        await self.sleep(self.config.wait_after_action)

        response = await self.wait_for_bodypass_condition(
            started_at,
            "bodypass_preview_close",
            lambda item: bodypass_contains_any(item, BODYPASS_PREVIEW_WINDOW_TEXTS),
        )
        await self.click_bodypass_toolbar_button(
            response,
            BODYPASS_CLOSE_TEXTS,
            BODYPASS_PREVIEW_CLOSE_OFFSET,
            "bodypass_preview_close",
        )
        await self.sleep(self.config.wait_after_action)

        response = await self.wait_for_bodypass_condition(
            started_at,
            "bodypass_detail_close",
            lambda item: bodypass_contains_any(item, BODYPASS_DETAIL_WINDOW_TEXTS)
            or bodypass_contains_any(item, BODYPASS_CLOSE_TEXTS),
        )
        await self.click_bodypass_toolbar_button(
            response,
            BODYPASS_CLOSE_TEXTS,
            BODYPASS_DETAIL_CLOSE_OFFSET,
            "bodypass_detail_close",
        )
        await self.sleep(self.config.wait_after_action)
        return "bodypass_finished"

    async def prepare_bodypass_main_window(self, started_at: float) -> dict[str, Any]:
        open_requested = False
        while True:
            self.check_runtime(started_at)
            response = await self.detect_window(f"bodypass_window_{self.capture_index + 1}.jpg")
            if is_bodypass_main_window(response):
                self.bodypass_main_box = bodypass_window_box(response)
                LOGGER.info("vision bodypass main window detected")
                return response
            if open_requested:
                LOGGER.info("vision bodypass not visible after open; wait")
                await self.sleep(self.config.wait_after_action)
                continue
            ok = await self.open_app(f"bodypass_open_{self.capture_index + 1}.jpg")
            if not ok:
                LOGGER.info("vision bodypass icon not found; wait %.1fs before retry", self.config.wait_after_no_detection)
                await self.sleep(self.config.wait_after_no_detection)
                continue
            open_requested = True
            await self.sleep(self.config.wait_after_open)

    async def input_bodypass_member(self, response: dict[str, Any], task: dict[str, Any]) -> None:
        window = bodypass_main_window(response) or response
        patient = task.get("patient", {}) if isinstance(task.get("patient"), dict) else {}
        patient_id = str(patient.get("patient_id") or task.get("scan_text") or "").strip()
        patient_name = str(patient.get("patient_name") or patient.get("name") or "").strip()

        id_center = bodypass_fixed_input_center(window, BODYPASS_MEMBER_ID_OFFSET)
        LOGGER.info("vision bodypass input patient_id at %s", id_center)
        await self.hid_output.input_text(patient_id, id_center[0], id_center[1], field="bodypass_patient_id")
        await self.sleep(self.config.wait_after_action)

        name_center = bodypass_fixed_input_center(window, BODYPASS_MEMBER_NAME_OFFSET)
        LOGGER.info("vision bodypass input patient_name at %s", name_center)
        await self.hid_output.input_text(patient_name, name_center[0], name_center[1], field="bodypass_patient_name")

    async def wait_for_bodypass_condition(
        self,
        started_at: float,
        stage: str,
        predicate: Any,
    ) -> dict[str, Any]:
        while True:
            self.check_runtime(started_at)
            response = await self.detect_window(f"{stage}_{self.capture_index + 1}.jpg")
            if predicate(response):
                LOGGER.info("vision bodypass stage=%s ready", stage)
                return response
            LOGGER.info("vision bodypass stage=%s wait", stage)
            await self.sleep(LINEAR_POLL_SECONDS)

    async def click_bodypass_toolbar_button(
        self,
        response: dict[str, Any],
        texts: tuple[str, ...],
        fallback_offset: tuple[int, int],
        action: str,
    ) -> None:
        box = self.bodypass_main_box or bodypass_window_box(response)
        if box is not None:
            center = (box[0] + fallback_offset[0], box[1] + fallback_offset[1])
        else:
            center = find_ocr_center_containing(response, *texts)
            if center is None:
                raise RuntimeError(f"BodyPass toolbar button not found: {action}")
        LOGGER.info("vision linear action=%s target=%s", action, center)
        await self.hid_output.click(center[0], center[1])

    async def prepare_new_patient_window(self, started_at: float) -> dict[str, Any]:
        open_requested = False
        while True:
            self.check_runtime(started_at)
            response = await self.detect_window(f"window_{self.capture_index + 1}.jpg")

            if is_new_patient_window(response):
                LOGGER.info("vision linear action=form_input target=None")
                return response

            if is_pdf_report_prompt(response):
                target = pdf_report_yes_target(response)
                if target is None:
                    raise RuntimeError("vision stale PDF report prompt found but no yes OCR target")
                LOGGER.info("vision linear action=click_text target=%s", target)
                await self.click_ocr_text_required(response, target)
                await self.sleep(self.config.wait_after_action)
                continue

            if is_report_generated(response):
                target = confirm_button_target(response)
                if target is not None:
                    LOGGER.info("vision linear action=click_text target=%s", target)
                    await self.click_ocr_text_required(response, target)
                else:
                    center = report_generated_visual_confirm_center(response.get("_image_path"))
                    if center is None:
                        raise RuntimeError("vision report generated found but no confirm target")
                    LOGGER.info("vision linear action=click_report_generated target=%s", center)
                    await self.hid_output.click(center[0], center[1])
                await self.sleep(self.config.wait_after_action)
                continue

            if is_login_window(response):
                center = login_button_center(response)
                if center is None:
                    raise RuntimeError("vision login window found but no login target")
                LOGGER.info("vision linear action=click_login target=%s", center)
                await self.hid_output.click(center[0], center[1])
                await self.sleep(self.config.wait_after_action)
                continue

            if is_ready_to_create_patient(response):
                LOGGER.info("vision linear action=click_text target=%s", NEW_PATIENT_TEXT)
                await self.click_ocr_text_required(response, NEW_PATIENT_TEXT)
                await self.sleep(self.config.wait_after_action)
                continue

            if has_no_detected_window(response):
                if open_requested:
                    LOGGER.info("vision no window after successful open; wait instead of opening again")
                    await self.sleep(self.config.wait_after_action)
                    continue
                ok = await self.open_app(f"open_{self.capture_index + 1}.jpg")
                if not ok:
                    LOGGER.info("vision icon not found; wait %.1fs before retry", self.config.wait_after_no_detection)
                    await self.sleep(self.config.wait_after_no_detection)
                    continue
                open_requested = True
                await self.sleep(self.config.wait_after_open)
                continue

            LOGGER.info("vision linear action=wait target=None stage=prepare")
            await self.sleep(self.config.wait_after_action)

    async def wait_and_click_start_check(self, started_at: float) -> None:
        while True:
            self.check_runtime(started_at)
            response = await self.detect_window(f"start_{self.capture_index + 1}.jpg")
            if is_ready_to_start_check(response):
                LOGGER.info("vision linear action=click_text target=%s", START_CHECK_TEXT)
                await self.click_ocr_text_required(response, START_CHECK_TEXT)
                await self.sleep(LINEAR_POLL_SECONDS)
                return
            LOGGER.info("vision linear action=wait target=None stage=start_check")
            await self.sleep(LINEAR_POLL_SECONDS)

    async def wait_and_click_data_analysis(self, started_at: float) -> None:
        while True:
            self.check_runtime(started_at)
            response = await self.detect_window(f"complete_{self.capture_index + 1}.jpg")
            if is_check_complete(response):
                LOGGER.info("vision linear action=click_text target=%s", ANALYSIS_TEXT)
                await self.click_ocr_text_required(response, ANALYSIS_TEXT)
                await self.sleep(max(self.config.analysis_wait, LINEAR_POLL_SECONDS))
                return
            LOGGER.info("vision linear action=wait target=None stage=check_complete")
            await self.sleep(LINEAR_POLL_SECONDS)

    async def wait_and_click_pdf_yes(self, started_at: float) -> None:
        while True:
            self.check_runtime(started_at)
            response = await self.detect_window(f"pdf_prompt_{self.capture_index + 1}.jpg")
            if is_pdf_report_prompt(response):
                target = pdf_report_yes_target(response)
                if target is None:
                    raise RuntimeError("vision PDF report prompt found but no yes OCR target")
                LOGGER.info("vision linear action=click_text target=%s", target)
                await self.click_ocr_text_required(response, target)
                await self.sleep(self.config.wait_after_action)
                return
            LOGGER.info("vision linear action=wait target=None stage=pdf_prompt")
            await self.sleep(LINEAR_POLL_SECONDS)

    async def wait_and_confirm_report_generated(self, started_at: float) -> None:
        while True:
            self.check_runtime(started_at)
            response = await self.detect_window(f"report_done_{self.capture_index + 1}.jpg")
            if is_report_generated(response):
                target = confirm_button_target(response)
                if target is not None:
                    LOGGER.info("vision linear action=click_text target=%s", target)
                    await self.click_ocr_text_required(response, target)
                else:
                    center = report_generated_visual_confirm_center(response.get("_image_path"))
                    if center is None:
                        raise RuntimeError("vision report generated found but no confirm target")
                    LOGGER.info("vision linear action=click_report_generated target=%s", center)
                    await self.hid_output.click(center[0], center[1])
                await self.sleep(self.config.wait_after_action)
                return
            LOGGER.info("vision linear action=wait target=None stage=report_generated")
            await self.sleep(LINEAR_POLL_SECONDS)

    async def wait_and_close_msc_explorer_after_report(self, started_at: float) -> None:
        deadline = time.monotonic() + MSC_EXPLORER_WAIT_SECONDS
        close_count = 0
        saw_popup = False
        while time.monotonic() < deadline:
            self.check_runtime(started_at)
            response = await self.detect_window(f"msc_popup_{self.capture_index + 1}.jpg")
            close_center = msc_explorer_close_center(response)
            if close_center is None:
                if saw_popup:
                    LOGGER.info("vision msc explorer popup closed")
                    return
                LOGGER.info("vision linear action=wait target=None stage=msc_explorer_popup")
                await self.sleep(MSC_EXPLORER_CLOSE_RETRY_SECONDS)
                continue
            close_count += 1
            saw_popup = True
            LOGGER.info("vision linear action=close_msc_explorer target=%s", close_center)
            await self.hid_output.click(close_center[0], close_center[1])
            await self.sleep(MSC_EXPLORER_AFTER_CLOSE_SECONDS)
            if close_count >= MSC_EXPLORER_MAX_CLOSES:
                LOGGER.info("vision msc explorer close max attempts reached")
                return
        LOGGER.info("vision msc explorer popup wait timeout")

    async def wait_and_click_new_patient_to_finish(self, started_at: float) -> None:
        while True:
            self.check_runtime(started_at)
            response = await self.detect_window(f"finish_{self.capture_index + 1}.jpg")
            if find_action_center(response, NEW_PATIENT_TEXT) is not None:
                LOGGER.info("vision linear action=click_text target=%s", NEW_PATIENT_TEXT)
                await self.click_ocr_text_required(response, NEW_PATIENT_TEXT)
                await self.sleep(self.config.wait_after_action)
                return
            LOGGER.info("vision linear action=wait target=None stage=finish")
            await self.sleep(LINEAR_POLL_SECONDS)

    async def detect_window(self, image_name: str) -> dict[str, Any]:
        close_count = 0
        while True:
            response = await self.capture_and_detect_window(image_name)
            if not bool(getattr(self.config, "close_msc_popup_when_detected", True)):
                return response
            close_center = msc_explorer_close_center(response)
            if close_center is None:
                return response
            if close_count >= MSC_EXPLORER_MAX_CLOSES:
                LOGGER.info("vision msc explorer close max attempts reached; continue with current response")
                return response
            close_count += 1
            LOGGER.info("vision linear action=close_msc_explorer target=%s", close_center)
            await self.hid_output.click(close_center[0], close_center[1])
            await self.sleep(MSC_EXPLORER_AFTER_CLOSE_SECONDS)

    async def detect_ocr_roi(self, image_name: str, roi_box: tuple[int, int, int, int]) -> dict[str, Any]:
        self.capture_index += 1
        image_path = self.workdir / image_name
        crop_path = image_path.with_name(f"{image_path.stem}_roi{image_path.suffix}")
        LOGGER.info("vision capture roi image: %s roi=%s", image_path, roi_box)
        await self.capture_screen(image_path)
        await to_thread(crop_jpeg_region, image_path, crop_path, roi_box)
        endpoint = ocr_endpoint_from_window_endpoint(self.config.window_endpoint)
        response = await to_thread(post_image, endpoint, crop_path, self.config.timeout_seconds)
        LOGGER.info("vision roi ocr response: %s", json.dumps(response, ensure_ascii=False))
        response["_image_path"] = str(crop_path)
        response["_source_image_path"] = str(image_path)
        response["_roi_box"] = list(roi_box)
        return response

    async def capture_and_detect_window(self, image_name: str) -> dict[str, Any]:
        self.capture_index += 1
        image_path = self.workdir / image_name
        LOGGER.info("vision capture window image: %s", image_path)
        await self.capture_screen(image_path)
        response = await to_thread(post_image, self.config.window_endpoint, image_path, self.config.timeout_seconds)
        LOGGER.info("vision window response: %s", json.dumps(response, ensure_ascii=False))
        response["_image_path"] = str(image_path)
        return response

    async def open_app(self, image_name: str) -> bool:
        image_path = self.workdir / image_name
        LOGGER.info("vision capture open image: %s", image_path)
        await self.capture_screen(image_path)
        response = await to_thread(
            post_image,
            self.config.icon_endpoint,
            image_path,
            self.config.timeout_seconds,
            {"software": self.config.software},
        )
        LOGGER.info("vision icon response: %s", json.dumps(response, ensure_ascii=False))
        center = extract_center(response, "center")
        if center is None:
            return False
        await self.hid_output.click(center[0], center[1])
        await self.sleep(0.12)
        await self.hid_output.click(center[0], center[1])
        return True

    async def click_ocr_text(self, response: dict[str, Any], text: str) -> bool:
        center = find_action_center(response, text)
        if center is None:
            return False
        await self.hid_output.click(center[0], center[1])
        return True

    async def click_ocr_text_any(self, response: dict[str, Any], texts: tuple[str, ...]) -> bool:
        center = find_ocr_center_containing(response, *texts)
        if center is None:
            return False
        await self.hid_output.click(center[0], center[1])
        return True

    async def click_ocr_text_required(self, response: dict[str, Any], text: str) -> None:
        if not await self.click_ocr_text(response, text):
            raise RuntimeError(f"vision OCR text not found: {text}")

    async def click_ocr_text_any_required(self, response: dict[str, Any], texts: tuple[str, ...]) -> None:
        if not await self.click_ocr_text_any(response, texts):
            raise RuntimeError(f"vision OCR text not found: {texts}")

    async def click_fixed(self, center: tuple[int, int], action: str) -> None:
        LOGGER.info("vision fixed action=%s target=%s", action, center)
        await self.hid_output.click(center[0], center[1])

    async def click_bodypass_close(self, response: dict[str, Any], action: str) -> None:
        center = find_ocr_center_containing(response, *BODYPASS_CLOSE_TEXTS)
        if center is None:
            box = first_window_box(response)
            if box is None:
                raise RuntimeError(f"BodyPass close target not found: {action}")
            center = (box[2] - 30, box[1] + 20)
        LOGGER.info("vision linear action=%s target=%s", action, center)
        await self.hid_output.click(center[0], center[1])

    async def click_bodypass_print_dialog(self, response: dict[str, Any]) -> None:
        center = find_ocr_center_containing(response, *BODYPASS_PRINT_DIALOG_TEXTS)
        if center is None:
            window = bodypass_print_dialog_window(response)
            box = extract_box(window) if window is not None else None
            if is_fullscreen_box(box):
                center = BODYPASS_PRINT_DIALOG_FULLSCREEN_PRINT_CENTER
            elif box is not None:
                center = (box[0] + BODYPASS_PRINT_DIALOG_PRINT_OFFSET[0], box[1] + BODYPASS_PRINT_DIALOG_PRINT_OFFSET[1])
            else:
                raise RuntimeError("BodyPass print dialog print button not found")
        LOGGER.info("vision linear action=bodypass_print_dialog_print target=%s", center)
        await self.hid_output.click(center[0], center[1])

    async def handle_analysis(self, response: dict[str, Any]) -> str | None:
        if not await self.click_ocr_text(response, ANALYSIS_TEXT):
            raise RuntimeError(f"vision OCR text not found: {ANALYSIS_TEXT}")
        await self.sleep(max(self.config.analysis_wait, ANALYSIS_NO_RESPONSE_WAIT_SECONDS))
        next_response = await self.detect_window(f"after_analysis_{self.capture_index + 1}.jpg")
        action, target = decide_after_analysis(next_response)
        LOGGER.info("vision after_analysis_action=%s target=%s", action, target)
        if action == "click_text" and target:
            if not await self.click_ocr_text(next_response, target):
                raise RuntimeError(f"vision OCR text not found: {target}")
            return "dialog_confirmed"
        if action == "finish":
            return "analysis_finished"
        LOGGER.info("vision analysis no response after %.1fs; finish flow", ANALYSIS_NO_RESPONSE_WAIT_SECONDS)
        return "analysis_finished"

    async def sleep(self, seconds: float) -> None:
        await asyncio.sleep(max(seconds, 0.0))
