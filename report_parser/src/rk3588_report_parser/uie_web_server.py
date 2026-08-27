"""Read-only UIE patient extraction web service for PC-first validation."""

from __future__ import annotations

import argparse
import json
import os
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Mapping, Optional, Sequence, Tuple, Type

from .clients import ServiceError
from .settings import load_settings, with_endpoint_overrides
from .uie_extraction import PaddleTaskflowEngine, UieRuntimeError, load_uie_schema, uie_prompts
from .uie_onnx import OnnxUieEngine
from .uie_rknn import RknnUieEngine
from .uie_patient_service import (
    CameraCaptureFileWatcher,
    MAX_CAPTURE_JSON_BYTES,
    UiePatientService,
    UiePatientServiceError,
)


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_IMAGE_BYTES + 1024 * 1024
MAX_SCHEMA_BYTES = 128 * 1024


def _asset(name: str) -> bytes:
    return resources.files("rk3588_report_parser.uie_web").joinpath(name).read_bytes()


def _multipart_image(content_type: str, body: bytes) -> Tuple[str, bytes]:
    if not content_type.lower().startswith("multipart/form-data"):
        raise ValueError("Content-Type must be multipart/form-data")
    message = BytesParser(policy=policy.default).parsebytes(
        ("Content-Type: %s\r\nMIME-Version: 1.0\r\n\r\n" % content_type).encode("ascii") + body
    )
    if not message.is_multipart():
        raise ValueError("multipart request is invalid")
    for part in message.iter_parts():
        if part.get_content_disposition() != "form-data":
            continue
        if part.get_param("name", header="content-disposition") != "image":
            continue
        filename = str(part.get_filename() or "report.jpg")
        payload = part.get_payload(decode=True) or b""
        if not payload:
            raise ValueError("image is empty")
        if len(payload) > MAX_IMAGE_BYTES:
            raise ValueError("image exceeds 20 MB")
        if Path(filename).suffix.lower() not in {".jpg", ".jpeg", ".png"}:
            raise ValueError("only JPEG and PNG are supported")
        return filename, payload
    raise ValueError("multipart request is missing image")


def make_handler(
    service: UiePatientService,
    access_token: str = "",
) -> Type[BaseHTTPRequestHandler]:
    class UieWebHandler(BaseHTTPRequestHandler):
        server_version = "RK3588PatientUIE/0.1"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _authorized(self) -> bool:
            if not access_token:
                return True
            return self.headers.get("Authorization", "") == "Bearer " + access_token

        def _is_loopback_client(self) -> bool:
            return str(self.client_address[0]) in {"127.0.0.1", "::1"}

        def _base_headers(self, content_type: str, length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; img-src 'self' blob:; style-src 'self'; "
                "script-src 'self'; connect-src 'self'",
            )

        def _send_json(self, status: int, payload: Mapping[str, Any]) -> None:
            body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
            self.send_response(status)
            self._base_headers("application/json; charset=utf-8", len(body))
            self.end_headers()
            self.wfile.write(body)

        def _send_asset(self, name: str, content_type: str) -> None:
            body = _asset(name)
            self.send_response(200)
            self._base_headers(content_type, len(body))
            self.end_headers()
            self.wfile.write(body)

        def _require_auth(self) -> bool:
            if self._authorized():
                return True
            self._send_json(401, {"status": "error", "error": "unauthorized"})
            return False

        def do_GET(self) -> None:
            path = self.path.split("?", 1)[0]
            public = {"/", "/assets/app.css", "/assets/app.js", "/favicon.ico", "/api/v1/health"}
            if path not in public and not self._require_auth():
                return
            if path == "/":
                self._send_asset("index.html", "text/html; charset=utf-8")
            elif path == "/assets/app.css":
                self._send_asset("app.css", "text/css; charset=utf-8")
            elif path == "/assets/app.js":
                self._send_asset("app.js", "application/javascript; charset=utf-8")
            elif path == "/favicon.ico":
                self.send_response(204)
                self._base_headers("image/x-icon", 0)
                self.end_headers()
            elif path == "/api/v1/health":
                self._send_json(200, {"ok": True, "service": "uie_patient_extractor"})
            elif path == "/api/v1/runtime":
                self._send_json(200, {"ok": True, **service.runtime_summary()})
            elif path == "/api/v1/schema":
                self._send_json(200, service.get_schema())
            elif path == "/api/v1/result":
                result = service.latest()
                if result is None:
                    self._send_json(200, {"status": "empty", "msg": "暂无识别结果"})
                else:
                    self._send_json(200, result)
            elif path == "/api/v1/patient":
                result = service.latest()
                if result is None:
                    self._send_json(
                        200,
                        {"code": "NO_RESULT", "data": [], "msg": "暂无识别结果", "success": False},
                    )
                else:
                    self._send_json(200, result["patient_response"])
            else:
                self._send_json(404, {"status": "error", "error": "not found"})

        def do_PUT(self) -> None:
            if not self._require_auth():
                return
            if self.path.split("?", 1)[0] != "/api/v1/schema":
                self._send_json(404, {"status": "error", "error": "not found"})
                return
            try:
                payload = self._read_json(MAX_SCHEMA_BYTES)
                configured = service.update_schema(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._send_json(400, {"status": "error", "error": str(exc)})
                return
            except (OSError, UieRuntimeError):
                self._send_json(500, {"status": "error", "error": "UIE配置保存失败"})
                return
            self._send_json(200, configured)

        def do_POST(self) -> None:
            path = self.path.split("?", 1)[0]
            if path == "/internal/v1/uie/extract":
                if not self._is_loopback_client() and not self._require_auth():
                    return
                try:
                    payload = self._read_json(MAX_CAPTURE_JSON_BYTES)
                    result = service.parse_capture(payload)
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    self._send_json(400, {"status": "error", "error": str(exc)})
                    return
                except UiePatientServiceError as exc:
                    self._send_json(429, {"status": "error", "error": str(exc)})
                    return
                except UieRuntimeError:
                    self._send_json(502, {"status": "error", "error": "UIE推理失败"})
                    return
                self._send_json(200, result)
                return

            if path == "/api/v1/result/select":
                if not self._require_auth():
                    return
                try:
                    payload = self._read_json(4096)
                    result = service.select_candidate(
                        str(payload.get("field_key", "")),
                        payload.get("candidate_index"),
                    )
                except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                    self._send_json(400, {"status": "error", "error": str(exc)})
                    return
                except OSError:
                    self._send_json(500, {"status": "error", "error": "复核结果保存失败"})
                    return
                self._send_json(200, result)
                return

            if not self._require_auth():
                return
            if path != "/api/v1/parse":
                self._send_json(404, {"status": "error", "error": "not found"})
                return
            try:
                length = self._content_length(MAX_REQUEST_BYTES)
                _, image_bytes = _multipart_image(
                    self.headers.get("Content-Type", ""), self.rfile.read(length)
                )
                result = service.parse_image(image_bytes)
            except ValueError as exc:
                self._send_json(400, {"status": "error", "error": str(exc)})
                return
            except UiePatientServiceError as exc:
                self._send_json(429, {"status": "error", "error": str(exc)})
                return
            except (ServiceError, UieRuntimeError):
                self._send_json(502, {"status": "error", "error": "本地OCR或UIE服务失败"})
                return
            except OSError:
                self._send_json(500, {"status": "error", "error": "本地识别失败"})
                return
            self._send_json(200, result)

        def _content_length(self, maximum: int) -> int:
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                raise ValueError("invalid Content-Length") from None
            if length < 1 or length > maximum:
                raise ValueError("request body size is invalid")
            return length

        def _read_json(self, maximum: int) -> dict[str, Any]:
            length = self._content_length(maximum)
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("request JSON must be an object")
            return payload

    return UieWebHandler


def create_server(
    host: str,
    port: int,
    service: UiePatientService,
    access_token: str = "",
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"} and not access_token:
        raise ValueError("a non-loopback listener requires an access token")
    server = ThreadingHTTPServer((host, port), make_handler(service, access_token))
    server.daemon_threads = True
    return server


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the UIE patient extraction web service")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8030)
    parser.add_argument("--ocr-endpoint")
    parser.add_argument("--schema", type=Path)
    parser.add_argument("--result-file", type=Path)
    parser.add_argument("--model", choices=("uie-base", "uie-medical-base"), default="uie-base")
    parser.add_argument("--engine", choices=("paddle", "onnx", "rknn"), default="paddle")
    parser.add_argument("--device", choices=("cpu", "gpu"), default="cpu")
    parser.add_argument("--position-prob", type=float, default=0.5)
    parser.add_argument("--max-seq-len", type=int, default=512)
    parser.add_argument("--onnx-model", type=Path)
    parser.add_argument("--onnx-vocab", type=Path)
    parser.add_argument("--onnx-threads", type=int, default=4)
    parser.add_argument("--rknn-model", type=Path)
    parser.add_argument("--rknn-vocab", type=Path)
    parser.add_argument("--rknn-sequence-length", type=int, choices=(128, 256, 512), default=256)
    parser.add_argument("--camera-result-file", type=Path)
    parser.add_argument("--camera-poll-seconds", type=float, default=0.5)
    parser.add_argument("--access-token-env", default="RK3588_UIE_ACCESS_TOKEN")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    project_root = Path(__file__).resolve().parents[2]
    schema_path = args.schema or project_root / "runtime" / "active_uie_schema.json"
    result_path = args.result_file or project_root / "runtime" / "latest_uie_patient.json"
    watcher: Optional[CameraCaptureFileWatcher] = None
    try:
        settings = with_endpoint_overrides(load_settings(args.config), args.ocr_endpoint, None)
        schema = load_uie_schema(schema_path if schema_path.is_file() else None)
        if args.engine == "rknn":
            model_path = args.rknn_model or project_root / "runtime" / "uie-base-rknn" / "uie-base-seq256-fp16.rknn"
            vocab_path = args.rknn_vocab or project_root / "runtime" / "uie-base-onnx" / "vocab.txt"
            engine = RknnUieEngine(
                model_path,
                vocab_path,
                uie_prompts(schema),
                position_prob=args.position_prob,
                sequence_length=args.rknn_sequence_length,
            )
            model_name = "%s-rknn-fp16" % args.model
        elif args.engine == "onnx":
            model_path = args.onnx_model or project_root / "runtime" / "uie-base-onnx" / "model.uint8.onnx"
            vocab_path = args.onnx_vocab or project_root / "runtime" / "uie-base-onnx" / "vocab.txt"
            engine = OnnxUieEngine(
                model_path,
                vocab_path,
                uie_prompts(schema),
                position_prob=args.position_prob,
                max_seq_len=args.max_seq_len,
                intra_op_threads=args.onnx_threads,
            )
            model_name = "%s-onnx%s" % (
                args.model,
                "-int8"
                if any(marker in model_path.name.lower() for marker in (".int8.", ".uint8."))
                else "",
            )
        else:
            engine = PaddleTaskflowEngine(
                args.model,
                uie_prompts(schema),
                device=args.device,
                position_prob=args.position_prob,
                max_seq_len=args.max_seq_len,
            )
            model_name = args.model
        service = UiePatientService(
            engine,
            schema,
            model_name,
            settings.ocr,
            schema_path=schema_path,
            result_path=result_path,
        )
        if args.camera_result_file is not None:
            watcher = CameraCaptureFileWatcher(
                service,
                args.camera_result_file,
                poll_seconds=args.camera_poll_seconds,
            )
        token = os.environ.get(args.access_token_env, "").strip()
        server = create_server(args.host, args.port, service, token)
    except (OSError, ValueError, UieRuntimeError) as exc:
        print("ERROR: %s" % exc, flush=True)
        return 2
    address, port = server.server_address[:2]
    print("UIE patient web ready: http://%s:%s" % (address, port), flush=True)
    if watcher is not None:
        watcher.start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        if watcher is not None:
            watcher.stop()
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
