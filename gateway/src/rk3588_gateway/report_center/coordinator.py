from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from .archive import ReportArchive
from .camera_patient import CameraPatientResolver
from .camera_runtime import retain_entry_image
from .connectors import ConnectorError, PatientConnectorService
from ..display_state import publish_display_state
from .domain import STANDARD_PATIENT_FIELDS, ConflictError, ValidationError, canonical_patient, validate_patient_mode
from .ocr_fields import RuleFieldResolver
from .store import ReportCenterStore


LOGGER = logging.getLogger(__name__)
EntryHandler = Callable[[str, dict[str, Any], dict[str, Any]], Awaitable[None]]


class ReportCenterCoordinator:
    def __init__(
        self,
        store: ReportCenterStore,
        archive: ReportArchive,
        connectors: PatientConnectorService,
        shadow_mode: bool,
        entry_handler: Optional[EntryHandler] = None,
        intake_only: bool = False,
        hid_active_marker: str = "",
        display_state_path: str = "",
        camera_configuration_image_dir: str = "",
        template_image_dir: str = "",
        entry_capture_dir: str = "",
    ) -> None:
        self.store = store
        self.archive = archive
        self.connectors = connectors
        self.shadow_mode = shadow_mode
        self.entry_handler = entry_handler
        self.intake_only = intake_only
        self.hid_active_marker = Path(hid_active_marker) if hid_active_marker else None
        self.display_state_path = str(display_state_path or "")
        self.camera_configuration_image_dir = str(camera_configuration_image_dir or "")
        self.template_image_dir = str(template_image_dir or "")
        self.entry_capture_dir = str(entry_capture_dir or "")
        self.field_resolver = RuleFieldResolver()
        self.camera_patient_resolver = CameraPatientResolver()
        self._stop = asyncio.Event()
        self._wake = asyncio.Event()
        self._clear_hid_active()
        self._publish_waiting()

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()

    def wake(self) -> None:
        self._wake.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                await self.process_queue_once()
            except asyncio.CancelledError:
                return
            except Exception:
                LOGGER.exception("report center queue cycle failed")
            self._wake.clear()
            try:
                await asyncio.wait_for(self._wake.wait(), timeout=0.5)
            except asyncio.TimeoutError:
                pass

    async def intake(self, payload: dict[str, Any], source: str = "api") -> dict[str, Any]:
        profile = self.store.active_profile_revision()
        config = profile["config"]
        mode = validate_patient_mode(str(payload.get("mode") or config.get("patient_input_mode", "manual")))
        capture_id = str(payload.get("capture_id", "")).strip() or None
        if capture_id:
            existing = self.store.get_session_by_capture(capture_id)
            if existing:
                return existing

        if mode in {"scanner_query", "camera_query"}:
            query = _query_from_payload(payload)
            records = await self._query_patients(config, query)
            records = _filter_exam_item(records, str(config.get("exam_item_filter", "")))
            if not records:
                session = self.store.create_session(
                    source, None, query, capture_id, "review_required", []
                )
            elif len(records) == 1:
                session = self.store.create_session(source, records[0], query, capture_id)
            else:
                session = self.store.create_session(
                    source, None, query, capture_id, "review_required", records
                )
        elif mode == "camera_direct":
            ocr = payload.get("ocr", payload)
            field_schema = config.get("field_resolver", {}).get("fields", [])
            resolved = self.field_resolver.resolve(ocr, field_schema)
            session_status = "queued" if resolved["status"] == "accepted" else resolved["status"]
            session = self.store.create_session(
                source, resolved["patient"], "", capture_id, session_status
            )
        else:
            patient = payload.get("patient")
            if not isinstance(patient, dict):
                raise ValidationError("manual intake requires patient")
            session = self.store.create_session(source, canonical_patient(patient), "", capture_id)
        if self.shadow_mode and session["status"] == "queued":
            session = self.store.transition_session(
                session["id"], "review_required", "shadow observation; no HID action"
            )
        self.store.audit("system", "session.intake", "session", session["id"], {"mode": mode, "source": source})
        self.wake()
        return session

    async def camera_capture(self, payload: dict[str, Any]) -> dict[str, Any]:
        capture, created = self.store.record_camera_capture(payload)
        capture_id = str(capture["capture_id"])
        if created:
            self._publish_report_detected(capture_id)
        profile = self.store.active_profile_revision()
        config = profile["config"]
        mode = str(config.get("patient_input_mode", "manual"))
        intake_enabled = bool(config.get("camera_intake_enabled", False))
        patient_result = self.store.latest_camera_patient_result(capture_id)
        if bool(config.get("camera_patient_enabled", False)) and patient_result is None:
            resolver_config = config.get("field_resolver", {})
            field_schema = resolver_config.get("fields", []) if isinstance(resolver_config, dict) else []
            resolved_patient = self.camera_patient_resolver.resolve(payload, field_schema)
            patient_result, _ = self.store.record_camera_patient_result(
                capture_id,
                int(profile["id"]),
                resolver_config if isinstance(resolver_config, dict) else {},
                resolved_patient,
                "camera",
            )
        patient_status = str(patient_result.get("status", "")) if patient_result else ""
        intake_blocked = patient_status in {"review_required", "rejected", "error"}
        if intake_blocked:
            self._publish_paper_reposition(capture_id)
        session = self.store.get_session_by_capture(capture_id)
        if intake_enabled and session is None and not intake_blocked:
            if mode not in {"camera_query", "camera_direct"}:
                raise ValidationError("camera intake requires camera_query or camera_direct mode")
            request = dict(payload)
            request["mode"] = mode
            session = await self.intake(request, source="camera")
            self.store.link_camera_capture_session(capture_id, session["id"])
        if session and session.get("status") in {"queued", "entering"}:
            self._publish_inputting(session.get("patient") or {})
        LOGGER.info(
            "camera capture observed capture=%s status=%s blocks=%d intake=%s",
            capture_id[:12], capture["status"], capture["block_count"],
            "enabled" if intake_enabled else "disabled",
        )
        if created:
            self.store.audit(
                "camera", "camera.capture_observed", "camera_capture", capture_id,
                {
                    "status": capture["status"],
                    "block_count": capture["block_count"],
                    "schema_version": capture["schema_version"],
                },
            )
        return {
            "ok": True,
            "created": created,
            "capture": {key: value for key, value in capture.items() if key != "payload"},
            "patient_intake": (
                "blocked" if intake_enabled and intake_blocked
                else "enabled" if intake_enabled
                else "disabled"
            ),
            "session": session,
            "patient_result": patient_result,
        }

    def preview_camera_patient(
        self,
        capture_id: str,
        resolver_config: dict[str, Any],
    ) -> dict[str, Any]:
        capture = self.store.get_camera_capture(capture_id)
        payload = capture.get("payload", {})
        fields = resolver_config.get("fields", []) if isinstance(resolver_config, dict) else []
        return self.camera_patient_resolver.resolve(payload, fields)

    def generate_camera_patient(
        self,
        capture_id: str,
        resolver_config: dict[str, Any],
        created_by: str,
    ) -> tuple[dict[str, Any], dict[str, Any], bool]:
        resolved = self.preview_camera_patient(capture_id, resolver_config)
        saved, created = self.store.record_camera_patient_result(
            capture_id,
            None,
            resolver_config,
            resolved,
            created_by,
        )
        return resolved, saved, created

    async def process_queue_once(self) -> Optional[dict[str, Any]]:
        session = self.store.next_queued_session(ignore_report_wait=self.intake_only)
        if session is None or self.shadow_mode:
            return session
        config = session["config_snapshot"]
        auto_entry = bool(config.get("auto_entry_enabled", True))
        if not auto_entry:
            self.store.transition_session(session["id"], "entering")
            target = "entry_completed" if self.intake_only else "awaiting_report"
            result = self.store.transition_session(session["id"], target)
            self._publish_waiting()
            return result
        if self.entry_handler is None:
            return self.store.transition_session(session["id"], "error", "HID entry handler is unavailable")
        session = self.store.transition_session(session["id"], "entering")
        steps = config.get("hid", {}).get("actions", [])
        if not isinstance(steps, list):
            steps = []
        run_id = self.store.begin_workflow_run(
            session["id"], steps, int(session["profile_revision_id"])
        )
        entry_log_id: Optional[int] = None
        try:
            entry_log_id = self._begin_entry_log(session, steps, run_id)
        except Exception:
            LOGGER.exception("failed to start entry log session=%s", session["id"][:12])
        self._publish_inputting(session.get("patient") or {})
        try:
            self._mark_hid_active(session["id"])
            patient = session.get("patient") or {}
            await self.entry_handler(str(session.get("query_code", "")), patient, config)
            self.store.finish_workflow_run(run_id, True, len(steps))
            self._finish_entry_log(entry_log_id, True)
            target = "entry_completed" if self.intake_only else "awaiting_report"
            result = self.store.transition_session(session["id"], target)
            self._publish_entry_completed(
                session.get("patient") or {},
                str(session.get("capture_id") or ""),
            )
            return result
        except Exception as exc:
            LOGGER.exception("patient HID entry failed session=%s", session["id"][:12])
            self.store.finish_workflow_run(run_id, False, 0, str(exc))
            self._finish_entry_log(entry_log_id, False, str(exc))
            result = self.store.transition_session(session["id"], "error", str(exc))
            self._publish_waiting()
            return result
        finally:
            self._clear_hid_active()

    def _mark_hid_active(self, session_id: str) -> None:
        if self.hid_active_marker is None:
            return
        self.hid_active_marker.parent.mkdir(parents=True, exist_ok=True)
        self.hid_active_marker.write_text(session_id, encoding="utf-8")
        os.chmod(self.hid_active_marker, 0o600)

    def _clear_hid_active(self) -> None:
        if self.hid_active_marker is not None:
            self.hid_active_marker.unlink(missing_ok=True)

    def _begin_entry_log(
        self,
        session: dict[str, Any],
        steps: Any,
        workflow_run_id: int,
    ) -> int:
        patient = session.get("patient") if isinstance(session.get("patient"), dict) else {}
        fields = {
            key: patient.get(key)
            for key in STANDARD_PATIENT_FIELDS
            if patient.get(key) not in (None, "")
        }
        extra_fields = patient.get("extra_fields")
        if isinstance(extra_fields, dict) and extra_fields:
            fields["extra_fields"] = extra_fields
        image_path = ""
        image_size = None
        image_sha256 = ""
        image_error = ""
        capture_id = str(session.get("capture_id") or "")
        if capture_id and self.entry_capture_dir and self.camera_configuration_image_dir:
            try:
                capture = self.store.get_camera_capture(capture_id)
                source = capture.get("payload", {}).get("source", {})
                expected = ""
                if isinstance(source, dict):
                    expected = str(
                        source.get("ocr_image_sha256")
                        or source.get("selected_frame_sha256")
                        or ""
                    )
                image = retain_entry_image(
                    capture_id,
                    self.camera_configuration_image_dir,
                    self.template_image_dir,
                    self.entry_capture_dir,
                    expected,
                )
                image_path = str(image)
                image_size = image.stat().st_size
                image_sha256 = _sha256_file(image)
            except Exception as exc:
                image_error = str(exc)[:1000]
                LOGGER.warning("entry image unavailable capture=%s reason=%s", capture_id[:12], image_error)
        return self.store.begin_entry_log(
            str(session["id"]),
            capture_id,
            patient,
            fields,
            len(steps) if isinstance(steps, list) else 0,
            workflow_run_id=workflow_run_id,
            image_path=image_path,
            image_size=image_size,
            image_sha256=image_sha256,
            image_error=image_error,
        )

    def _publish_inputting(self, patient: dict[str, Any]) -> None:
        self._publish_display({
            "screen": "inputting",
            "patient_name": str(patient.get("patient_name") or ""),
            "patient_id": str(patient.get("patient_id") or ""),
        })

    def _publish_report_detected(self, capture_id: str) -> None:
        self._publish_display({
            "screen": "report_detecting",
            "capture_id": capture_id,
            "progress_text": "生成结构化字段",
        })

    def _publish_paper_reposition(self, capture_id: str) -> None:
        self._publish_display({
            "screen": "paper_reposition",
            "capture_id": capture_id,
        })

    def _publish_entry_completed(self, patient: dict[str, Any], capture_id: str = "") -> None:
        self._publish_display(
            {
                "screen": "entry_completed",
                "patient_name": str(patient.get("patient_name") or ""),
                "patient_id": str(patient.get("patient_id") or ""),
                "capture_id": capture_id,
            },
            expires_at=time.time() + 3.0,
        )

    def _publish_waiting(self) -> None:
        self._publish_display({"screen": "wait_scan", "patient_name": "", "patient_id": ""})

    def _finish_entry_log(
        self,
        entry_log_id: Optional[int],
        success: bool,
        error: str = "",
    ) -> None:
        if entry_log_id is None:
            return
        try:
            self.store.finish_entry_log(entry_log_id, success, error)
        except Exception:
            LOGGER.exception("failed to finish entry log id=%s", entry_log_id)

    def _publish_display(self, display: dict[str, Any], **state: Any) -> None:
        try:
            publish_display_state(self.display_state_path, display, **state)
        except OSError:
            LOGGER.debug("shared display state is unavailable", exc_info=True)

    def receive_reports(self, paths: list[str], source: str) -> list[dict[str, Any]]:
        session = None if self.shadow_mode else self.store.active_report_session()
        if session:
            expected = str(session["config_snapshot"].get("report_source", ""))
            if expected and expected != source:
                session = None
        upload_target = self._upload_target(session)
        reports = self.archive.ingest_batch(paths, source, session, "system", upload_target)
        self.wake()
        return reports

    def approve(self, session_id: str, patient: dict[str, Any], actor: str) -> dict[str, Any]:
        if self.shadow_mode:
            raise ConflictError("patient approval is disabled in shadow mode")
        session = self.store.approve_session(session_id, patient)
        self.store.audit(actor, "session.approve", "session", session_id)
        self.wake()
        return session

    def _upload_target(self, session: Optional[dict[str, Any]]) -> Optional[dict[str, Any]]:
        if not session:
            return None
        connector_id = session["config_snapshot"].get("upload_target_id")
        return self.store.get_connector(int(connector_id)) if connector_id else None

    async def _query_patients(self, config: dict[str, Any], query: str) -> list[dict[str, Any]]:
        connector_id = config.get("patient_connector_id")
        if not connector_id:
            raise ValidationError("published profile has no patient connector")
        connector = self.store.get_connector(int(connector_id))
        if not connector["enabled"]:
            raise ConnectorError("patient connector is disabled")
        return await self.connectors.query(connector, query)


def _query_from_payload(payload: dict[str, Any]) -> str:
    for key in ("code", "identifier", "candidate_number"):
        value = payload.get(key)
        if isinstance(value, dict):
            value = value.get("value")
        text = str(value or "").strip()
        if text:
            return text
    candidates = payload.get("candidates", [])
    if isinstance(candidates, list) and candidates:
        candidate = candidates[0]
        if isinstance(candidate, dict):
            value = str(candidate.get("value", "")).strip()
            if value:
                return value
    raise ValidationError("patient query code is required")


def _filter_exam_item(records: list[dict[str, Any]], expected: str) -> list[dict[str, Any]]:
    expected = expected.strip()
    if not expected:
        return records
    matches = [record for record in records if str(record.get("exam_item") or "").strip() == expected]
    return matches


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()
