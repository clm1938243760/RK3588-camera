from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from pathlib import Path
from typing import Any, Optional

from aiohttp import ClientSession, ClientTimeout, FormData

from .config import ReportCenterConfig
from .connectors import json_path_get
from ..display_state import publish_display_state
from .store import ReportCenterStore


LOGGER = logging.getLogger(__name__)


class ReportCenterUploadWorker:
    def __init__(
        self,
        config: ReportCenterConfig,
        store: ReportCenterStore,
        display_state_path: str = "",
    ) -> None:
        self.config = config
        self.store = store
        self.display_state_path = str(display_state_path or "")
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_ready()
            except asyncio.CancelledError:
                return
            except Exception:
                LOGGER.exception("report center upload cycle failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=self.config.upload_poll_seconds)
            except asyncio.TimeoutError:
                pass

    async def process_ready(self, one_only: bool = False) -> int:
        processed = 0
        while not self._stop.is_set():
            job = self.store.next_upload_job(self.config.upload_max_attempts)
            if job is None:
                break
            self._publish_uploading()
            success, status, error, response = await self._upload(job)
            self.store.finish_upload(
                int(job["id"]), success, self.config.upload_max_attempts,
                self.config.upload_retry_seconds, status, error, response,
            )
            self._publish_upload_result(success, error)
            processed += 1
            if one_only:
                break
        return processed

    async def _upload(self, job: dict[str, Any]) -> tuple[bool, Optional[int], str, str]:
        path = Path(str(job["archive_path"]))
        if not path.is_file():
            return False, None, "archived PDF is missing", ""
        if _sha256(path) != str(job["pdf_sha256"]):
            return False, None, "archived PDF hash changed", ""
        target = job.get("target_snapshot", {})
        target_type = str(target.get("type", ""))
        config = target.get("config", {})
        if target_type not in {"report_multipart", "rest_multipart"} or not isinstance(config, dict):
            return False, None, "upload target snapshot is invalid", ""
        endpoint = str(config.get("endpoint", "")).strip()
        if not endpoint.startswith(("http://", "https://")):
            return False, None, "upload endpoint is invalid", ""
        headers = config.get("headers", {})
        if not isinstance(headers, dict):
            return False, None, "upload headers are invalid", ""
        form = FormData()
        report_field = str(config.get("report_field", "Report"))
        info_field = str(config.get("report_info_field", "ReportInfo"))
        form.add_field(report_field, path.read_bytes(), filename=path.name, content_type="application/pdf")
        info = bytes(job.get("report_info_xml") or b"")
        if info or target_type == "report_multipart":
            form.add_field(info_field, info, filename="ReportInfo.xml", content_type="application/xml")
        timeout = ClientTimeout(total=max(1, int(config.get("timeout_seconds", 30))))
        try:
            async with ClientSession(
                timeout=timeout,
                headers={str(key): str(value) for key, value in headers.items()},
            ) as session:
                async with session.post(endpoint, data=form) as result:
                    status = int(result.status)
                    text = await result.text()
        except asyncio.TimeoutError:
            return False, None, "upload timed out", ""
        except Exception as exc:
            return False, None, "upload failed: %s" % type(exc).__name__, ""
        success, error = _is_success(status, text, config)
        return success, status, error, text

    def _publish_uploading(self) -> None:
        self._publish_display({"screen": "report_uploading"})

    def _publish_upload_result(self, success: bool, error: str = "") -> None:
        self._publish_display(
            {
                "screen": "report_upload_success" if success else "report_upload_failed",
                "upload_error": str(error or "")[:240],
            },
            expires_at=time.time() + 3.0,
        )

    def _publish_display(self, display: dict[str, Any], **state: Any) -> None:
        if not self.display_state_path:
            return
        try:
            publish_display_state(self.display_state_path, display, **state)
        except OSError:
            LOGGER.debug("shared display state is unavailable", exc_info=True)


def _is_success(status: int, text: str, config: dict[str, Any]) -> tuple[bool, str]:
    allowed = config.get("success_http_status", list(range(200, 300)))
    if isinstance(allowed, int):
        allowed = [allowed]
    if status not in {int(value) for value in allowed}:
        return False, "upload returned HTTP %d" % status
    path = str(config.get("success_json_path", "")).strip()
    if not path:
        try:
            payload = json.loads(text)
        except json.JSONDecodeError:
            return True, ""
        if isinstance(payload, dict) and (payload.get("success") is False or str(payload.get("code", "")).upper() in {"FAIL", "FAILED", "ERROR"}):
            return False, "upload target rejected report"
        return True, ""
    try:
        payload = json.loads(text)
        actual = json_path_get(payload, path)
    except (ValueError, json.JSONDecodeError):
        return False, "upload success response is invalid"
    expected = config.get("success_value", True)
    return (True, "") if actual == expected else (False, "upload success condition did not match")


def _sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
