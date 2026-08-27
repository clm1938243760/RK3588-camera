from __future__ import annotations

import argparse
import base64
import io
import json
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Protocol, Sequence, Tuple, Type

from PIL import Image


class DesktopOcrRuntimeError(RuntimeError):
    pass


class OcrEngineProtocol(Protocol):
    def recognize(self, image_bytes: bytes) -> List[Dict[str, Any]]:
        ...


def _number(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    return None


def _as_sequence(value: Any) -> Optional[List[Any]]:
    if isinstance(value, (str, bytes, bytearray, dict)):
        return None
    try:
        return list(value)
    except TypeError:
        return None


def _is_polygon(value: Any) -> bool:
    points = _as_sequence(value)
    if points is None or len(points) < 4:
        return False
    for point in points:
        coordinates = _as_sequence(point)
        if coordinates is None or len(coordinates) < 2:
            return False
        if _number(coordinates[0]) is None or _number(coordinates[1]) is None:
            return False
    return True


def _is_text_score(value: Any) -> bool:
    values = _as_sequence(value)
    return (
        values is not None
        and len(values) >= 2
        and isinstance(values[0], str)
        and _number(values[1]) is not None
    )


def _walk_paddle_lines(value: Any) -> Iterable[Tuple[Sequence[Any], Sequence[Any]]]:
    """Yield v2 PaddleOCR ``(polygon, (text, score))`` pairs recursively."""

    items = _as_sequence(value)
    if items is not None:
        if len(items) == 2 and _is_polygon(items[0]) and _is_text_score(items[1]):
            yield items[0], items[1]
            return
        for child in items:
            yield from _walk_paddle_lines(child)


def normalize_paddle_result(value: Any) -> List[Dict[str, Any]]:
    """Convert PaddleOCR v2 output to the stable local /ocr response shape."""

    items: List[Dict[str, Any]] = []
    for polygon, text_score in _walk_paddle_lines(value):
        text = str(text_score[0]).strip()
        score = _number(text_score[1])
        if not text or score is None:
            continue
        points = [[round(float(point[0]), 2), round(float(point[1]), 2)] for point in polygon]
        left = min(point[0] for point in points)
        top = min(point[1] for point in points)
        right = max(point[0] for point in points)
        bottom = max(point[1] for point in points)
        items.append(
            {
                "text": text,
                "score": max(0.0, min(1.0, float(score))),
                "box": [round(left, 2), round(top, 2), round(right, 2), round(bottom, 2)],
                "polygon": points,
            }
        )
    return items


class PaddleOcrEngine:
    """PaddleOCR 2.x adapter for the existing loopback-only /ocr contract."""

    def __init__(
        self,
        det_model_dir: Optional[Path],
        rec_model_dir: Optional[Path],
        cls_model_dir: Optional[Path],
        bootstrap_models: bool,
        language: str,
        use_angle_cls: bool,
    ) -> None:
        if not bootstrap_models and (det_model_dir is None or rec_model_dir is None):
            raise DesktopOcrRuntimeError(
                "provide --det-model-dir and --rec-model-dir, or explicitly use --bootstrap-models"
            )
        try:
            import numpy as np
            from paddleocr import PaddleOCR
        except ImportError as exc:
            raise DesktopOcrRuntimeError(
                "PaddleOCR desktop dependencies are missing. Create a separate Python 3.10 environment "
                "and install requirements-pc-ocr.txt."
            ) from exc

        self._np = np
        self._use_angle_cls = use_angle_cls
        options: Dict[str, Any] = {
            "use_angle_cls": use_angle_cls,
            "lang": language,
            "show_log": False,
        }
        if det_model_dir is not None:
            options["det_model_dir"] = str(det_model_dir)
        if rec_model_dir is not None:
            options["rec_model_dir"] = str(rec_model_dir)
        if cls_model_dir is not None:
            options["cls_model_dir"] = str(cls_model_dir)
        try:
            self._ocr = PaddleOCR(**options)
        except Exception as exc:
            raise DesktopOcrRuntimeError("could not initialize local PaddleOCR models: %s" % exc) from exc
        self._lock = threading.Lock()

    def recognize(self, image_bytes: bytes) -> List[Dict[str, Any]]:
        try:
            with Image.open(io.BytesIO(image_bytes)) as image:
                rgb = image.convert("RGB")
                array = self._np.asarray(rgb)
        except Exception as exc:
            raise DesktopOcrRuntimeError("request image cannot be decoded") from exc
        # PaddleOCR 2.x expects an OpenCV-style BGR ndarray.
        bgr = array[:, :, ::-1].copy()
        try:
            with self._lock:
                result = self._ocr.ocr(bgr, cls=self._use_angle_cls)
        except Exception as exc:
            raise DesktopOcrRuntimeError("local PaddleOCR inference failed: %s" % exc) from exc
        return normalize_paddle_result(result)


def _read_json_request(handler: BaseHTTPRequestHandler) -> Dict[str, Any]:
    try:
        length = int(handler.headers.get("Content-Length", "0"))
    except ValueError as exc:
        raise ValueError("invalid Content-Length") from exc
    if length < 1 or length > 24 * 1024 * 1024:
        raise ValueError("request body size is invalid")
    try:
        payload = json.loads(handler.rfile.read(length).decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise ValueError("request body must be JSON") from exc
    if not isinstance(payload, dict):
        raise ValueError("request JSON must be an object")
    return payload


def _decode_image(payload: Dict[str, Any]) -> bytes:
    value = payload.get("image_base64")
    if not isinstance(value, str) or not value.strip():
        raise ValueError("image_base64 must be a non-empty string")
    try:
        image = base64.b64decode(value.encode("ascii"), validate=True)
    except (UnicodeEncodeError, ValueError) as exc:
        raise ValueError("image_base64 is invalid") from exc
    if not image or len(image) > 18 * 1024 * 1024:
        raise ValueError("decoded image size is invalid")
    return image


def make_handler(engine: OcrEngineProtocol) -> Type[BaseHTTPRequestHandler]:
    class LocalPpOcrHandler(BaseHTTPRequestHandler):
        server_version = "RK3588ReportParserDesktopOCR/0.1"

        def log_message(self, format: str, *args: object) -> None:
            # Request data contains report content and must never reach logs.
            return

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self) -> None:
            if self.path == "/health":
                self._send_json(200, {"ok": True, "backend": "paddleocr_desktop", "local_only": True})
                return
            self._send_json(404, {"error": {"message": "not found"}})

        def do_POST(self) -> None:
            if self.path != "/ocr":
                self._send_json(404, {"error": {"message": "not found"}})
                return
            try:
                items = engine.recognize(_decode_image(_read_json_request(self)))
            except (DesktopOcrRuntimeError, ValueError) as exc:
                self._send_json(400, {"ok": False, "error": {"message": str(exc)}})
                return
            except Exception:
                self._send_json(500, {"ok": False, "error": {"message": "local OCR inference failed"}})
                return
            self._send_json(200, {"ok": True, "ocr": items})

    return LocalPpOcrHandler


def create_server(host: str, port: int, engine: OcrEngineProtocol) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("desktop OCR server must bind to a loopback address")
    server = ThreadingHTTPServer((host, port), make_handler(engine))
    server.daemon_threads = True
    return server


def _path_or_none(value: Optional[str]) -> Optional[Path]:
    if value is None or not value.strip():
        return None
    path = Path(value).expanduser()
    if not path.is_dir():
        raise DesktopOcrRuntimeError("model directory does not exist: %s" % path)
    return path


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run a local PaddleOCR 2.x server for report-parser desktop tests")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=5002)
    parser.add_argument("--det-model-dir")
    parser.add_argument("--rec-model-dir")
    parser.add_argument("--cls-model-dir")
    parser.add_argument(
        "--bootstrap-models",
        action="store_true",
        help="explicitly allow PaddleOCR to obtain its default local model files on first launch",
    )
    parser.add_argument("--language", default="ch")
    parser.add_argument("--disable-angle-cls", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    try:
        engine = PaddleOcrEngine(
            det_model_dir=_path_or_none(args.det_model_dir),
            rec_model_dir=_path_or_none(args.rec_model_dir),
            cls_model_dir=_path_or_none(args.cls_model_dir),
            bootstrap_models=args.bootstrap_models,
            language=args.language,
            use_angle_cls=not args.disable_angle_cls,
        )
        server = create_server(args.host, args.port, engine)
    except (DesktopOcrRuntimeError, OSError, ValueError) as exc:
        print("ERROR: %s" % exc, flush=True)
        return 2

    address, port = server.server_address[:2]
    print("Local OCR server ready: http://%s:%s/health" % (address, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
