from __future__ import annotations

import argparse
import json
import os
import threading
from dataclasses import replace
from email import policy
from email.parser import BytesParser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib import resources
from pathlib import Path
from typing import Any, Dict, Optional, Sequence, Tuple, Type

from .clients import ServiceError
from .identifier_pipeline import IdentifierParser
from .identifier_rules import IdentifierRuleSettings, parse_identifier_rule_settings
from .manifest import ManifestError, check_manifest
from .settings import ParserSettings, load_settings, with_endpoint_overrides


MAX_IMAGE_BYTES = 20 * 1024 * 1024
MAX_REQUEST_BYTES = MAX_IMAGE_BYTES + 1024 * 1024


class WebRuntimeError(RuntimeError):
    pass


class ParserService:
    def __init__(
        self,
        parser: IdentifierParser,
        queue_size: int = 4,
        rules_path: Optional[Path] = None,
    ) -> None:
        self.parser = parser
        self._inference_lock = threading.Lock()
        self._slots = threading.BoundedSemaphore(queue_size)
        self._rules_path = rules_path

    def parse(self, image_bytes: bytes) -> Dict[str, Any]:
        if not self._slots.acquire(blocking=False):
            raise WebRuntimeError("inference queue is full")
        try:
            with self._inference_lock:
                return self.parser.parse_bytes(image_bytes).result.to_dict()
        finally:
            self._slots.release()

    def get_rules(self) -> Dict[str, Any]:
        return self.parser.settings.identifier_rules.to_dict()

    def rule_summary(self) -> Dict[str, Any]:
        return self.parser.settings.identifier_rules.summary()

    def update_rules(self, payload: Dict[str, Any]) -> Dict[str, Any]:
        settings = parse_identifier_rule_settings(payload)
        with self._inference_lock:
            if self._rules_path is not None:
                self._rules_path.parent.mkdir(parents=True, exist_ok=True)
                temporary = self._rules_path.with_suffix(self._rules_path.suffix + ".tmp")
                temporary.write_text(
                    json.dumps(settings.to_dict(), ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8",
                )
                temporary.replace(self._rules_path)
            self.parser.settings = replace(self.parser.settings, identifier_rules=settings)
        return settings.to_dict()


def _asset(name: str) -> bytes:
    return resources.files("rk3588_report_parser.web").joinpath(name).read_bytes()


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
        suffix = Path(filename).suffix.lower()
        if suffix not in {".jpg", ".jpeg", ".png"}:
            raise ValueError("only JPEG and PNG are supported")
        return filename, payload
    raise ValueError("multipart request is missing image")


def make_handler(
    service: ParserService,
    runtime: Dict[str, Any],
    access_token: str = "",
) -> Type[BaseHTTPRequestHandler]:
    class ReportWebHandler(BaseHTTPRequestHandler):
        server_version = "RK3588IdentifierWeb/0.5"

        def log_message(self, format: str, *args: object) -> None:
            return

        def _authorized(self) -> bool:
            if not access_token:
                return True
            return self.headers.get("Authorization", "") == "Bearer " + access_token

        def _base_headers(self, content_type: str, length: int) -> None:
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(length))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("Referrer-Policy", "no-referrer")
            self.send_header("Content-Security-Policy", "default-src 'self'; img-src 'self' blob:; style-src 'self'; script-src 'self'; connect-src 'self'")

        def _send_json(self, status: int, payload: Dict[str, Any]) -> None:
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
            public_paths = {"/", "/assets/app.css", "/assets/app.js", "/favicon.ico", "/api/v1/health"}
            if path not in public_paths and not self._require_auth():
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
                self._send_json(200, {"ok": True, "service": "identifier_parser", "profile": runtime["profile"]})
            elif path == "/api/v1/runtime":
                payload = dict(runtime)
                if hasattr(service, "rule_summary"):
                    payload["identifier_rules"] = service.rule_summary()
                self._send_json(200, payload)
            elif path == "/api/v1/rules":
                if not hasattr(service, "get_rules"):
                    self._send_json(501, {"status": "error", "error": "rule configuration is unavailable"})
                else:
                    self._send_json(200, service.get_rules())
            else:
                self._send_json(404, {"status": "error", "error": "not found"})

        def do_PUT(self) -> None:
            if not self._require_auth():
                return
            if self.path.split("?", 1)[0] != "/api/v1/rules":
                self._send_json(404, {"status": "error", "error": "not found"})
                return
            if not hasattr(service, "update_rules"):
                self._send_json(501, {"status": "error", "error": "rule configuration is unavailable"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 2 or length > 128 * 1024:
                    raise ValueError("rule request body size is invalid")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                if not isinstance(payload, dict):
                    raise ValueError("rule request must be a JSON object")
                configured = service.update_rules(payload)
            except (UnicodeDecodeError, json.JSONDecodeError, ValueError) as exc:
                self._send_json(400, {"status": "error", "error": str(exc)})
                return
            except OSError:
                self._send_json(500, {"status": "error", "error": "rule configuration could not be saved"})
                return
            self._send_json(200, configured)

        def do_POST(self) -> None:
            if not self._require_auth():
                return
            if self.path.split("?", 1)[0] != "/api/v1/parse":
                self._send_json(404, {"status": "error", "error": "not found"})
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
            except ValueError:
                self._send_json(400, {"status": "error", "error": "invalid Content-Length"})
                return
            if length < 1 or length > MAX_REQUEST_BYTES:
                self._send_json(413, {"status": "error", "error": "request body is too large"})
                return
            try:
                _, image_bytes = _multipart_image(
                    self.headers.get("Content-Type", ""), self.rfile.read(length)
                )
                payload = service.parse(image_bytes)
            except ValueError as exc:
                self._send_json(400, {"status": "error", "error": str(exc)})
                return
            except WebRuntimeError as exc:
                self._send_json(429, {"status": "error", "error": str(exc)})
                return
            except (OSError, ServiceError) as exc:
                self._send_json(502, {"status": "error", "error": str(exc)})
                return
            except Exception:
                self._send_json(500, {"status": "error", "error": "local parsing failed"})
                return
            self._send_json(200, payload)

    return ReportWebHandler


def create_server(
    host: str,
    port: int,
    service: ParserService,
    runtime: Dict[str, Any],
    access_token: str = "",
) -> ThreadingHTTPServer:
    if host not in {"127.0.0.1", "localhost", "::1"} and not access_token:
        raise ValueError("a non-loopback listener requires an access token")
    server = ThreadingHTTPServer((host, port), make_handler(service, runtime, access_token))
    server.daemon_threads = True
    return server


def _runtime_check(path: Path) -> Dict[str, Any]:
    try:
        return check_manifest(path)
    except ManifestError as exc:
        return {"ok": False, "errors": [str(exc)]}


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Run the local application-form identifier web service")
    parser.add_argument("--config", type=Path)
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8020)
    parser.add_argument("--ocr-endpoint")
    parser.add_argument("--llm-endpoint")
    parser.add_argument("--access-token-env", default="RK3588_REPORT_ACCESS_TOKEN")
    parser.add_argument(
        "--rules-file",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "runtime" / "active_identifier_rules.json",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=Path(__file__).resolve().parents[2] / "runtime" / "manifest.json",
    )
    parser.add_argument("--allow-unverified-runtime", action="store_true")
    return parser


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    manifest = _runtime_check(args.manifest)
    if not manifest.get("ok") and not args.allow_unverified_runtime:
        print("ERROR: runtime manifest is not verified", flush=True)
        return 2
    try:
        settings = with_endpoint_overrides(
            load_settings(args.config), args.ocr_endpoint, args.llm_endpoint
        )
        if args.rules_file.is_file():
            saved_rules = json.loads(args.rules_file.read_text(encoding="utf-8"))
            settings = replace(
                settings,
                identifier_rules=parse_identifier_rule_settings(saved_rules),
            )
        token = os.environ.get(args.access_token_env, "").strip()
        runtime = {
            "ok": bool(manifest.get("ok")),
            "profile": settings.profile,
            "model": settings.llm.model,
            "ocr_backend": "local_ppocr",
            "manifest": manifest,
            "image_retention": "none",
            "max_image_bytes": MAX_IMAGE_BYTES,
        }
        service = ParserService(IdentifierParser(settings), rules_path=args.rules_file)
        server = create_server(args.host, args.port, service, runtime, token)
    except (OSError, ValueError) as exc:
        print("ERROR: %s" % exc, flush=True)
        return 2
    address, port = server.server_address[:2]
    print("Identifier web ready: http://%s:%s" % (address, port), flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        return 0
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
