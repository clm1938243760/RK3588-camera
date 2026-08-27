from __future__ import annotations

import argparse
import hashlib
import ipaddress
import json
import logging
import os
import ssl
import time
from pathlib import Path
from typing import Any, Callable
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import Request, urlopen


LOGGER = logging.getLogger(__name__)
FINAL_STATUSES = {"accepted", "review_required", "rejected", "error"}
MAX_PAYLOAD_BYTES = 5 * 1024 * 1024
Sender = Callable[[str, dict[str, Any], float, bool], dict[str, Any]]


class PermanentForwardError(RuntimeError):
    pass


def validate_capture_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise ValueError("capture result must be a JSON object")
    capture_id = str(payload.get("capture_id", "")).strip()
    if not capture_id or len(capture_id) > 128:
        raise ValueError("capture_id is required")
    if str(payload.get("status", "")) not in FINAL_STATUSES:
        raise ValueError("capture result is not final")
    document = payload.get("document")
    if not isinstance(document, dict) or int(document.get("schema_version", 0)) != 2:
        raise ValueError("OCR document schema_version 2 is required")
    if not isinstance(document.get("blocks"), list) or not isinstance(document.get("lines"), list):
        raise ValueError("OCR blocks and lines must be arrays")
    return payload


def payload_digest(payload: dict[str, Any]) -> str:
    encoded = json.dumps(
        payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def ensure_loopback_endpoint(endpoint: str) -> None:
    parsed = urlparse(endpoint)
    if parsed.scheme not in {"http", "https"} or not parsed.hostname:
        raise ValueError("report-center endpoint must be HTTP or HTTPS")
    if parsed.hostname.lower() == "localhost":
        return
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ValueError("report-center endpoint must use a loopback host") from exc
    if not address.is_loopback:
        raise ValueError("report-center endpoint must use a loopback host")


def send_capture(
    endpoint: str,
    payload: dict[str, Any],
    timeout_seconds: float,
    allow_insecure_loopback_tls: bool,
) -> dict[str, Any]:
    ensure_loopback_endpoint(endpoint)
    body = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    if len(body) > MAX_PAYLOAD_BYTES:
        raise PermanentForwardError("camera capture payload exceeds 5 MiB")
    request = Request(
        endpoint,
        data=body,
        method="POST",
        headers={"Content-Type": "application/json", "User-Agent": "rk3588-camera-forwarder/1"},
    )
    context = None
    if endpoint.lower().startswith("https://") and allow_insecure_loopback_tls:
        context = ssl._create_unverified_context()
    try:
        with urlopen(request, timeout=timeout_seconds, context=context) as response:
            response_body = response.read(1024 * 1024)
    except HTTPError as exc:
        if 400 <= exc.code < 500 and exc.code not in {408, 429}:
            raise PermanentForwardError("report center rejected capture with HTTP %d" % exc.code) from exc
        raise RuntimeError("report center returned HTTP %d" % exc.code) from exc
    except (URLError, TimeoutError, OSError) as exc:
        raise RuntimeError("report center is unavailable: %s" % type(exc).__name__) from exc
    try:
        result = json.loads(response_body.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise RuntimeError("report center returned invalid JSON") from exc
    if not isinstance(result, dict) or result.get("ok") is not True:
        raise RuntimeError("report center did not acknowledge capture")
    return result


class CameraCaptureForwarder:
    def __init__(
        self,
        source_file: str,
        state_file: str,
        endpoint: str,
        timeout_seconds: float = 5.0,
        poll_seconds: float = 0.5,
        allow_insecure_loopback_tls: bool = False,
        sender: Sender = send_capture,
    ) -> None:
        ensure_loopback_endpoint(endpoint)
        self.source_file = Path(source_file)
        self.state_file = Path(state_file)
        self.endpoint = endpoint
        self.timeout_seconds = max(0.5, timeout_seconds)
        self.poll_seconds = max(0.1, poll_seconds)
        self.allow_insecure_loopback_tls = allow_insecure_loopback_tls
        self.sender = sender

    def run_once(self) -> str:
        try:
            payload = self._read_capture()
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            LOGGER.debug("camera result is not ready: %s", type(exc).__name__)
            return "not_ready"
        digest = payload_digest(payload)
        capture_id = str(payload["capture_id"])
        state = self._read_state()
        if state.get("payload_sha256") == digest and state.get("status") in {
            "forwarded", "permanent_failure"
        }:
            return "unchanged"
        now = time.time()
        if state.get("payload_sha256") == digest and float(state.get("next_attempt_at", 0)) > now:
            return "retry_wait"
        attempts = int(state.get("attempts", 0)) + 1 if state.get("payload_sha256") == digest else 1
        try:
            response = self.sender(
                self.endpoint, payload, self.timeout_seconds, self.allow_insecure_loopback_tls
            )
        except PermanentForwardError as exc:
            self._write_state({
                "capture_id": capture_id,
                "payload_sha256": digest,
                "status": "permanent_failure",
                "attempts": attempts,
                "last_error": str(exc)[:300],
                "updated_at": now,
            })
            LOGGER.error("camera capture rejected capture=%s reason=%s", capture_id[:12], exc)
            return "permanent_failure"
        except Exception as exc:
            retry_delay = min(30.0, float(2 ** min(attempts - 1, 5)))
            self._write_state({
                "capture_id": capture_id,
                "payload_sha256": digest,
                "status": "retry_wait",
                "attempts": attempts,
                "next_attempt_at": now + retry_delay,
                "last_error": str(exc)[:300],
                "updated_at": now,
            })
            LOGGER.warning(
                "camera capture forwarding failed capture=%s attempt=%d reason=%s",
                capture_id[:12], attempts, type(exc).__name__,
            )
            return "retry_wait"
        self._write_state({
            "capture_id": capture_id,
            "payload_sha256": digest,
            "status": "forwarded",
            "attempts": attempts,
            "report_center_created": bool(response.get("created")),
            "updated_at": time.time(),
        })
        document = payload["document"]
        LOGGER.info(
            "camera capture forwarded capture=%s status=%s blocks=%d",
            capture_id[:12], payload["status"], len(document["blocks"]),
        )
        return "forwarded"

    def run(self) -> None:
        while True:
            self.run_once()
            time.sleep(self.poll_seconds)

    def _read_capture(self) -> dict[str, Any]:
        if self.source_file.stat().st_size > MAX_PAYLOAD_BYTES:
            raise ValueError("camera result exceeds 5 MiB")
        with self.source_file.open("r", encoding="utf-8") as handle:
            return validate_capture_payload(json.load(handle))

    def _read_state(self) -> dict[str, Any]:
        try:
            with self.state_file.open("r", encoding="utf-8") as handle:
                value = json.load(handle)
            return value if isinstance(value, dict) else {}
        except (OSError, ValueError, json.JSONDecodeError):
            return {}

    def _write_state(self, state: dict[str, Any]) -> None:
        self.state_file.parent.mkdir(parents=True, exist_ok=True)
        temporary = self.state_file.with_name(
            ".%s.%d.tmp" % (self.state_file.name, os.getpid())
        )
        try:
            with temporary.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, ensure_ascii=False, separators=(",", ":"))
                handle.flush()
                os.fsync(handle.fileno())
            os.chmod(temporary, 0o600)
            os.replace(temporary, self.state_file)
        finally:
            try:
                temporary.unlink()
            except FileNotFoundError:
                pass


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Forward final camera OCR evidence to report center")
    parser.add_argument(
        "--source-file", default="/run/rk3588-report-parser/verified-full-text.json"
    )
    parser.add_argument(
        "--state-file", default="/run/rk3588-report-parser/report-center-forwarder.json"
    )
    parser.add_argument(
        "--endpoint", default="https://127.0.0.1:8443/internal/v1/camera-captures"
    )
    parser.add_argument("--timeout-seconds", type=float, default=5.0)
    parser.add_argument("--poll-seconds", type=float, default=0.5)
    parser.add_argument("--allow-insecure-loopback-tls", action="store_true")
    parser.add_argument("--once", action="store_true")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    forwarder = CameraCaptureForwarder(
        source_file=args.source_file,
        state_file=args.state_file,
        endpoint=args.endpoint,
        timeout_seconds=args.timeout_seconds,
        poll_seconds=args.poll_seconds,
        allow_insecure_loopback_tls=args.allow_insecure_loopback_tls,
    )
    if args.once:
        return 1 if forwarder.run_once() in {"retry_wait", "permanent_failure"} else 0
    forwarder.run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
