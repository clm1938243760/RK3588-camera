from __future__ import annotations

import asyncio
import copy
import ipaddress
import logging
import os
import ssl
import time
from pathlib import Path
from typing import Any, Awaitable, Callable, Optional

from aiohttp import web

from .archive import ReportArchive
from .auth import WebSession, WebSessionStore
from .camera_runtime import (
    find_configuration_image,
    full_page_once_status,
    request_full_page_once,
    retain_configuration_image,
    valid_normalized_roi,
    write_runtime_template,
)
from .config import ReportCenterConfig
from .connectors import ConnectorError, PatientConnectorService, render_sql_template
from .coordinator import ReportCenterCoordinator
from .domain import (
    PATIENT_INPUT_MODES,
    REPORT_SOURCES,
    STANDARD_PATIENT_FIELDS,
    ConflictError,
    NotFoundError,
    ReportCenterError,
    ValidationError,
)
from .portal import PORTAL_HTML
from .store import ReportCenterStore
from .upload import ReportCenterUploadWorker


LOGGER = logging.getLogger(__name__)
HardwareStatus = Callable[[], Awaitable[dict[str, Any]]]
COOKIE_NAME = "rk3588_report_center_session"


class ReportCenterWeb:
    def __init__(
        self,
        config: ReportCenterConfig,
        store: ReportCenterStore,
        coordinator: ReportCenterCoordinator,
        archive: ReportArchive,
        connectors: PatientConnectorService,
        upload_worker: ReportCenterUploadWorker,
        hardware_status: Optional[HardwareStatus] = None,
    ) -> None:
        self.config = config
        self.store = store
        self.coordinator = coordinator
        self.archive = archive
        self.connectors = connectors
        self.upload_worker = upload_worker
        self.hardware_status = hardware_status
        self.sessions = WebSessionStore(config.session_hours)
        self.login_failures: dict[str, list[float]] = {}
        self.app = web.Application(middlewares=[self.error_middleware, self.auth_middleware])
        self.app.add_routes(self._routes())
        self.runner: Optional[web.AppRunner] = None

    def _routes(self) -> list[Any]:
        return [
            web.get("/", self.portal),
            web.get("/assets/{path:.*}", self.portal_asset),
            web.get("/health", self.health),
            web.get("/status", self.compat_status),
            web.post("/scan", self.compat_scan),
            web.post("/patient/query", self.compat_patient_query),
            web.post("/api/v1/auth/login", self.login),
            web.post("/api/v1/auth/logout", self.logout),
            web.get("/api/v1/auth/session", self.session_info),
            web.post("/api/v1/auth/password", self.change_password),
            web.get("/api/v1/status", self.status),
            web.post("/api/v1/intake", self.intake),
            web.get("/api/v1/sessions", self.list_sessions),
            web.get("/api/v1/sessions/{id}", self.get_session),
            web.post("/api/v1/sessions/{id}/cancel", self.cancel_session),
            web.post("/api/v1/sessions/{id}/mark-missing", self.mark_missing),
            web.post("/api/v1/sessions/{id}/retry-entry", self.retry_entry),
            web.post("/api/v1/sessions/{id}/approve", self.approve_session),
            web.get("/api/v1/camera/configuration-capture", self.configuration_capture_status),
            web.post("/api/v1/camera/configuration-capture", self.request_configuration_capture),
            web.get("/api/v1/camera-captures", self.list_camera_captures),
            web.get("/api/v1/camera-captures/{capture_id}", self.get_camera_capture),
            web.get("/api/v1/camera-captures/{capture_id}/image", self.get_camera_capture_image),
            web.post("/api/v1/camera-captures/{capture_id}/retain-image", self.retain_camera_capture_image),
            web.get("/api/v1/camera-captures/{capture_id}/patient", self.get_camera_patient),
            web.post("/api/v1/camera-captures/{capture_id}/resolve-patient", self.resolve_camera_patient),
            web.get("/api/v1/camera-patient", self.get_latest_camera_patient),
            web.get("/api/v1/entry-logs", self.list_entry_logs),
            web.get("/api/v1/entry-logs/{id}", self.get_entry_log),
            web.get("/api/v1/entry-logs/{id}/image", self.get_entry_log_image),
            web.get("/api/v1/reports", self.list_reports),
            web.get("/api/v1/reports/{id}", self.get_report),
            web.get("/api/v1/reports/{id}/revisions", self.report_revisions),
            web.get("/api/v1/reports/{id}/content", self.report_content),
            web.patch("/api/v1/reports/{id}/metadata", self.revise_metadata),
            web.post("/api/v1/reports/{id}/assign", self.assign_report),
            web.post("/api/v1/reports/{id}/retry-upload", self.retry_upload),
            web.get("/api/v1/profiles", self.list_profiles),
            web.get("/api/v1/profiles/{id}", self.get_profile),
            web.post("/api/v1/profiles", self.create_profile),
            web.put("/api/v1/profiles/{id}/draft", self.save_profile_draft),
            web.post("/api/v1/profiles/{id}/test", self.test_profile),
            web.post("/api/v1/profiles/{id}/publish", self.publish_profile),
            web.post("/api/v1/profiles/{id}/rollback", self.rollback_profile),
            web.get("/api/v1/connectors", self.list_connectors),
            web.post("/api/v1/connectors", self.create_connector),
            web.put("/api/v1/connectors/{id}", self.update_connector),
            web.post("/api/v1/connectors/{id}/test", self.test_connector),
            web.get("/api/v1/hardware", self.hardware),
            web.get("/api/v1/audit", self.audit),
            web.get("/api/v1/users", self.list_users),
            web.post("/api/v1/users", self.create_user),
            web.get("/api/v1/tokens", self.list_tokens),
            web.post("/api/v1/tokens", self.create_token),
            web.delete("/api/v1/tokens/{id}", self.revoke_token),
            web.post("/api/v1/maintenance/cleanup", self.cleanup),
            web.post("/api/v1/maintenance/backup", self.backup),
            web.post("/internal/v1/camera-captures", self.camera_capture),
            web.get("/internal/v1/field-rules", self.internal_field_rules),
            web.post("/internal/v1/field-rules", self.update_internal_field_rules),
            web.get("/internal/v1/field-result", self.internal_field_result),
            web.post("/internal/v1/reports", self.internal_reports),
            web.post("/internal/v1/external-reports", self.external_report),
            web.get("/archive/v1/reports", self.archive_reports),
            web.get("/archive/v1/reports/{id}", self.archive_report),
            web.get("/archive/v1/reports/{id}/content", self.archive_content),
        ]

    @web.middleware
    async def error_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        try:
            return await handler(request)
        except web.HTTPException:
            raise
        except NotFoundError as exc:
            return web.json_response({"error": str(exc)}, status=404)
        except ConflictError as exc:
            return web.json_response({"error": str(exc)}, status=409)
        except (ValidationError, ConnectorError) as exc:
            return web.json_response({"error": str(exc)}, status=400)
        except ReportCenterError as exc:
            return web.json_response({"error": str(exc)}, status=500)
        except Exception:
            LOGGER.exception("report center request failed path=%s", request.path)
            return web.json_response({"error": "internal server error"}, status=500)

    @web.middleware
    async def auth_middleware(self, request: web.Request, handler: Any) -> web.StreamResponse:
        path = request.path
        if path in {"/", "/health", "/status", "/api/v1/auth/login"} or path.startswith("/assets/"):
            return await handler(request)
        if path.startswith("/archive/v1/"):
            return await handler(request)
        if path.startswith("/internal/v1/") or path in {"/scan", "/patient/query"}:
            if not _is_loopback(request.remote):
                return web.json_response({"error": "loopback access only"}, status=403)
            return await handler(request)
        token = request.cookies.get(COOKIE_NAME, "")
        session = self.sessions.get(token)
        if session is None:
            return web.json_response({"error": "authentication required"}, status=401)
        request["session"] = session
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            if session.must_change and path not in {"/api/v1/auth/password", "/api/v1/auth/logout"}:
                return web.json_response({"error": "initial password must be changed first"}, status=403)
            supplied = request.headers.get("X-CSRF-Token", "")
            if not supplied or not secrets_compare(supplied, session.csrf):
                return web.json_response({"error": "CSRF validation failed"}, status=403)
        return await handler(request)

    async def start(self) -> None:
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        context = _ssl_context(self.config)
        site = web.TCPSite(self.runner, self.config.host, self.config.port, ssl_context=context)
        await site.start()
        LOGGER.info(
            "report center web listening on %s://%s:%d",
            "https" if context else "http", self.config.host, self.config.port,
        )

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def portal(self, request: web.Request) -> web.Response:
        built = Path(self.config.portal_dir) / "index.html"
        if built.is_file():
            return web.FileResponse(
                built, headers={"Cache-Control": "no-cache, no-store, must-revalidate"}
            )
        return web.Response(
            text=PORTAL_HTML,
            content_type="text/html",
            headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
        )

    async def portal_asset(self, request: web.Request) -> web.StreamResponse:
        root = (Path(self.config.portal_dir) / "assets").resolve()
        path = (root / request.match_info["path"]).resolve()
        try:
            inside = os.path.commonpath([str(path), str(root)]) == str(root)
        except ValueError:
            inside = False
        if not inside or not path.is_file():
            raise web.HTTPNotFound()
        return web.FileResponse(path)

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "service": "rk3588-report-center", "shadow_mode": self.config.shadow_mode})

    async def compat_status(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "shadow_mode": self.config.shadow_mode, "counts": self.store.counts()})

    async def login(self, request: web.Request) -> web.Response:
        remote = request.remote or ""
        now = time.time()
        failures = [value for value in self.login_failures.get(remote, []) if now - value < 300]
        self.login_failures[remote] = failures
        if len(failures) >= 10:
            return web.json_response({"error": "too many login attempts"}, status=429)
        payload = await _json_body(request)
        user = self.store.verify_user(str(payload.get("username", "")), str(payload.get("password", "")))
        if user is None:
            failures.append(now)
            return web.json_response({"error": "用户名或密码错误"}, status=401)
        self.login_failures.pop(remote, None)
        session = self.sessions.create(user)
        self.store.audit(session.username, "auth.login", "user", str(user["id"]), remote=remote)
        response = web.json_response(_session_payload(session))
        response.set_cookie(
            COOKIE_NAME, session.token, httponly=True, secure=bool(_ssl_context(self.config)),
            samesite="Strict", max_age=self.config.session_hours * 3600, path="/",
        )
        return response

    async def logout(self, request: web.Request) -> web.Response:
        session = _web_session(request)
        self.sessions.remove(request.cookies.get(COOKIE_NAME, ""))
        self.store.audit(session.username, "auth.logout", "user", remote=request.remote or "")
        response = web.json_response({"ok": True})
        response.del_cookie(COOKIE_NAME, path="/")
        return response

    async def session_info(self, request: web.Request) -> web.Response:
        return web.json_response(_session_payload(_web_session(request)))

    async def change_password(self, request: web.Request) -> web.Response:
        session = _web_session(request)
        payload = await _json_body(request)
        current = str(payload.get("current_password", ""))
        if self.store.verify_user(session.username, current) is None:
            raise ValidationError("current password is incorrect")
        self.store.set_password(session.username, str(payload.get("new_password", "")))
        session.must_change = False
        self.store.audit(session.username, "auth.password_change", "user", remote=request.remote or "")
        return web.json_response({"ok": True})

    async def status(self, request: web.Request) -> web.Response:
        active = self.store.list_sessions(20)
        current = next((item for item in active if item["status"] in {"entering", "awaiting_report", "archiving"}), None)
        return web.json_response({
            "shadow_mode": self.config.shadow_mode,
            "counts": self.store.counts(),
            "current_session": current,
            "profile": self.store.active_profile_revision(),
        })

    async def intake(self, request: web.Request) -> web.Response:
        payload = await _json_body(request)
        session = await self.coordinator.intake(payload, source="web")
        return web.json_response(session, status=201)

    async def compat_scan(self, request: web.Request) -> web.Response:
        payload = await _json_body(request)
        payload["mode"] = "scanner_query"
        session = await self.coordinator.intake(payload, source="scanner_api")
        return web.json_response({"ok": True, "id": session["id"], "status": session["status"]})

    async def compat_patient_query(self, request: web.Request) -> web.Response:
        payload = await _json_body(request)
        query = str(payload.get("code", "")).strip()
        profile = self.store.active_profile_revision()["config"]
        connector_id = profile.get("patient_connector_id")
        if not connector_id:
            return web.json_response({"code": "FAIL", "data": [], "msg": "未配置患者连接器", "success": False}, status=503)
        records = await self.connectors.query(self.store.get_connector(int(connector_id)), query)
        return web.json_response({"code": "SUCCESS", "data": records, "msg": "成功", "success": True})

    async def list_sessions(self, request: web.Request) -> web.Response:
        return web.json_response({"items": self.store.list_sessions(request.query.get("limit", 100), request.query.get("status", ""))})

    async def get_session(self, request: web.Request) -> web.Response:
        return web.json_response(self.store.get_session(request.match_info["id"]))

    async def cancel_session(self, request: web.Request) -> web.Response:
        session = _web_session(request)
        current = self.store.get_session(request.match_info["id"])
        if current["status"] in {"entering", "archiving"}:
            raise ConflictError("an active HID or archive operation cannot be cancelled")
        result = self.store.transition_session(request.match_info["id"], "cancelled")
        self.store.audit(session.username, "session.cancel", "session", result["id"])
        self.coordinator.wake()
        return web.json_response(result)

    async def mark_missing(self, request: web.Request) -> web.Response:
        session = _web_session(request)
        result = self.store.transition_session(request.match_info["id"], "report_missing")
        self.store.audit(session.username, "session.mark_missing", "session", result["id"])
        self.coordinator.wake()
        return web.json_response(result)

    async def retry_entry(self, request: web.Request) -> web.Response:
        session = _web_session(request)
        result = self.store.transition_session(request.match_info["id"], "queued")
        self.store.audit(session.username, "session.retry_entry", "session", result["id"])
        self.coordinator.wake()
        return web.json_response(result)

    async def approve_session(self, request: web.Request) -> web.Response:
        user = _web_session(request)
        payload = await _json_body(request)
        current = self.store.get_session(request.match_info["id"])
        patient = payload.get("patient")
        if not isinstance(patient, dict):
            index = int(payload.get("candidate_index", -1))
            choices = current.get("review_candidates", [])
            if index < 0 or index >= len(choices):
                raise ValidationError("patient or candidate_index is required")
            patient = choices[index]
        return web.json_response(self.coordinator.approve(current["id"], patient, user.username))

    async def list_camera_captures(self, request: web.Request) -> web.Response:
        return web.json_response({
            "items": self.store.list_camera_captures(request.query.get("limit", 100))
        })

    async def configuration_capture_status(self, request: web.Request) -> web.Response:
        return web.json_response(
            full_page_once_status(self.config.camera_full_page_once_file)
        )

    async def request_configuration_capture(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        result = request_full_page_once(
            self.config.camera_full_page_once_file, user.username
        )
        self.store.audit(
            user.username,
            "camera.configuration_capture_requested",
            "camera",
            "full_page_once",
        )
        return web.json_response(result, status=202)

    async def get_camera_capture(self, request: web.Request) -> web.Response:
        return web.json_response(self.store.get_camera_capture(request.match_info["capture_id"]))

    async def get_camera_capture_image(self, request: web.Request) -> web.StreamResponse:
        capture_id = request.match_info["capture_id"]
        self.store.get_camera_capture(capture_id, include_payload=False)
        path = find_configuration_image(
            capture_id,
            self.config.camera_configuration_image_dir,
            self.config.template_image_dir,
        )
        if path is None:
            raise web.HTTPNotFound(text="configuration image is unavailable")
        return web.FileResponse(path, headers={
            "Content-Type": "image/jpeg",
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
        })

    async def retain_camera_capture_image(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        capture_id = request.match_info["capture_id"]
        capture = self.store.get_camera_capture(capture_id)
        source = capture.get("payload", {}).get("source", {})
        expected = str(source.get("ocr_image_sha256", "")) if isinstance(source, dict) else ""
        path = retain_configuration_image(
            capture_id,
            self.config.camera_configuration_image_dir,
            self.config.template_image_dir,
            expected,
        )
        self.store.audit(user.username, "camera.image_retained", "camera_capture", capture_id)
        return web.json_response({"ok": True, "capture_id": capture_id, "size": path.stat().st_size})

    async def get_camera_patient(self, request: web.Request) -> web.Response:
        result = self.store.latest_camera_patient_result(request.match_info["capture_id"])
        return web.json_response(_camera_patient_response(result))

    async def get_latest_camera_patient(self, request: web.Request) -> web.Response:
        result = self.store.latest_camera_patient_result_any()
        return web.json_response(_camera_patient_response(result))

    async def list_entry_logs(self, request: web.Request) -> web.Response:
        filters = dict(request.query)
        return web.json_response(self.store.list_entry_logs(filters))

    async def get_entry_log(self, request: web.Request) -> web.Response:
        log_id = int(request.match_info["id"])
        result = self.store.get_entry_log(log_id)
        result["image_url"] = (
            "/api/v1/entry-logs/%d/image" % log_id if result["image_available"] else ""
        )
        result["download_url"] = (
            "/api/v1/entry-logs/%d/image?download=1" % log_id if result["image_available"] else ""
        )
        return web.json_response(result)

    async def get_entry_log_image(self, request: web.Request) -> web.StreamResponse:
        user = _web_session(request)
        log_id = int(request.match_info["id"])
        entry_log = self.store.get_entry_log(log_id, include_private_path=True)
        path = Path(str(entry_log.get("image_path", ""))).resolve()
        root = Path(self.config.entry_capture_dir or ".").resolve()
        try:
            inside = os.path.commonpath([str(path), str(root)]) == str(root)
        except ValueError:
            inside = False
        if not inside or not path.is_file():
            raise NotFoundError("entry capture image is unavailable")
        attachment = request.query.get("download") == "1"
        disposition = "attachment" if attachment else "inline"
        self.store.audit(
            user.username,
            "entry_log.image_download" if attachment else "entry_log.image_view",
            "entry_log",
            str(log_id),
            remote=request.remote or "",
        )
        return web.FileResponse(path, headers={
            "Content-Type": "image/jpeg",
            "Content-Disposition": "%s; filename*=UTF-8''entry-%d.jpg" % (disposition, log_id),
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Length": str(path.stat().st_size),
        })

    async def resolve_camera_patient(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        resolver_config = payload.get("field_resolver")
        if not isinstance(resolver_config, dict):
            resolver_config = self.store.active_profile_revision()["config"].get("field_resolver", {})
        _validate_field_resolver(resolver_config)
        capture_id = request.match_info["capture_id"]
        persist = bool(payload.get("persist", False))
        if persist:
            result, saved, created = self.coordinator.generate_camera_patient(
                capture_id, resolver_config, user.username
            )
            result = dict(result)
            result.update({"persisted": True, "created": created, "result_id": saved["id"]})
        else:
            result = self.coordinator.preview_camera_patient(capture_id, resolver_config)
        self.store.audit(
            user.username,
            "camera.patient_generated" if persist else "camera.patient_preview",
            "camera_capture", capture_id,
            {"status": result["status"], "field_count": len(result["evidence"])},
        )
        return web.json_response(result)

    async def list_reports(self, request: web.Request) -> web.Response:
        return web.json_response(self.store.list_reports(dict(request.query)))

    async def get_report(self, request: web.Request) -> web.Response:
        return web.json_response(self.store.get_report(int(request.match_info["id"])))

    async def report_revisions(self, request: web.Request) -> web.Response:
        return web.json_response({"items": self.store.list_report_revisions(int(request.match_info["id"]))})

    async def report_content(self, request: web.Request) -> web.StreamResponse:
        user = _web_session(request)
        report = self.store.get_report(int(request.match_info["id"]))
        response = _file_response(report, self.config.archive_dir, attachment=request.query.get("download") == "1")
        self.store.audit(user.username, "report.download", "report", str(report["id"]), remote=request.remote or "")
        return response

    async def revise_metadata(self, request: web.Request) -> web.Response:
        user = _web_session(request)
        payload = await _json_body(request)
        report_id = int(request.match_info["id"])
        current = self.store.get_report(report_id)
        reason = str(payload.get("reason", "")).strip()
        if current.get("upload_status") == "uploaded":
            _require_admin(user)
            if not reason:
                raise ValidationError("uploaded report correction requires a reason")
        metadata = dict(current.get("metadata", {}))
        changes = payload.get("metadata", {})
        if not isinstance(changes, dict):
            raise ValidationError("metadata must be an object")
        metadata.update(changes)
        result = self.store.revise_report_metadata(report_id, metadata, reason, user.username)
        self.store.audit(user.username, "report.metadata_revise", "report", str(report_id), {"reason": reason})
        return web.json_response(result)

    async def assign_report(self, request: web.Request) -> web.Response:
        user = _web_session(request)
        payload = await _json_body(request)
        return web.json_response(self.store.assign_report(int(request.match_info["id"]), str(payload.get("session_id", "")), user.username))

    async def retry_upload(self, request: web.Request) -> web.Response:
        user = _web_session(request)
        report_id = int(request.match_info["id"])
        if not self.store.retry_upload(report_id):
            raise ConflictError("report has no retryable upload job")
        self.store.audit(user.username, "upload.retry", "report", str(report_id))
        self.upload_worker.wake()
        return web.json_response({"ok": True})

    async def list_profiles(self, request: web.Request) -> web.Response:
        return web.json_response({"items": self.store.list_profiles()})

    async def get_profile(self, request: web.Request) -> web.Response:
        return web.json_response(self.store.get_profile(int(request.match_info["id"])))

    async def create_profile(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        config = payload.get("config", {})
        _validate_profile(config, self.store)
        profile_id = self.store.create_profile(str(payload.get("name", "")), config, user.username)
        self.store.audit(user.username, "profile.create", "profile", str(profile_id))
        return web.json_response({"id": profile_id}, status=201)

    async def save_profile_draft(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        config = payload.get("config", {})
        _validate_profile(config, self.store)
        await self._retain_profile_reference_image(config, user.username)
        revision_id = self.store.save_profile_draft(int(request.match_info["id"]), config, user.username)
        self.store.audit(user.username, "profile.draft", "profile", request.match_info["id"], {"revision_id": revision_id})
        return web.json_response({"revision_id": revision_id})

    async def test_profile(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        config = payload.get("config")
        if not isinstance(config, dict):
            profile = self.store.get_profile(int(request.match_info["id"]))
            draft = next((item for item in profile["revisions"] if item["status"] == "draft"), profile["revisions"][0])
            config = draft["config"]
        details = _validate_profile(config, self.store)
        return web.json_response({"ok": True, "checks": details})

    async def publish_profile(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        revision = self.store.publish_profile(int(request.match_info["id"]), user.username)
        runtime = write_runtime_template(
            self.config.camera_template_runtime_file,
            self.store.active_profile_revision(),
        )
        self.store.audit(user.username, "profile.publish", "profile", request.match_info["id"], {"revision_id": revision})
        return web.json_response({"revision_id": revision, "camera_template": runtime})

    async def rollback_profile(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        revision = self.store.publish_profile(int(request.match_info["id"]), user.username, int(payload.get("revision_id", 0)))
        write_runtime_template(
            self.config.camera_template_runtime_file,
            self.store.active_profile_revision(),
        )
        self.store.audit(user.username, "profile.rollback", "profile", request.match_info["id"], {"revision_id": revision})
        return web.json_response({"revision_id": revision})

    async def _retain_profile_reference_image(self, config: dict[str, Any], actor: str) -> None:
        template = config.get("camera_template", {})
        if not isinstance(template, dict):
            return
        capture_id = str(template.get("reference_capture_id", "")).strip()
        if not capture_id:
            return
        capture = self.store.get_camera_capture(capture_id)
        source = capture.get("payload", {}).get("source", {})
        expected = str(source.get("ocr_image_sha256", "")) if isinstance(source, dict) else ""
        retain_configuration_image(
            capture_id,
            self.config.camera_configuration_image_dir,
            self.config.template_image_dir,
            expected,
        )
        self.store.audit(actor, "camera.image_retained", "camera_capture", capture_id)

    async def list_connectors(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        return web.json_response({"items": self.store.list_connectors()})

    async def create_connector(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        connector_type = str(payload.get("type", ""))
        connector_config = payload.get("config", {})
        _validate_connector_definition(connector_type, connector_config)
        connector_id = self.store.create_connector(str(payload.get("name", "")), connector_type, connector_config)
        self.store.audit(user.username, "connector.create", "connector", str(connector_id))
        return web.json_response({"id": connector_id}, status=201)

    async def update_connector(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        connector_id = int(request.match_info["id"])
        current = self.store.get_connector(connector_id)
        connector_config = payload.get("config", current["config"])
        _validate_connector_definition(current["type"], connector_config)
        result = self.store.update_connector(
            connector_id, str(payload.get("name", current["name"])),
            connector_config, bool(payload.get("enabled", current["enabled"])),
        )
        self.store.audit(user.username, "connector.update", "connector", str(connector_id))
        return web.json_response(result)

    async def test_connector(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        connector = self.store.get_connector(int(request.match_info["id"]))
        if connector["type"] not in {"sql_proxy", "rest_json"}:
            raise ValidationError("upload targets are tested by a real queued report")
        result = await self.connectors.test(connector, str(payload.get("query", "")))
        self.store.audit(user.username, "connector.test", "connector", request.match_info["id"], {"ok": True})
        return web.json_response(result)

    async def hardware(self, request: web.Request) -> web.Response:
        if self.hardware_status:
            return web.json_response(await self.hardware_status())
        return web.json_response({"camera": "external", "ocr": "external", "hid": "configured", "report_sources": ["msc", "printer"]})

    async def audit(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        return web.json_response({"items": self.store.list_audit(int(request.query.get("limit", 200)))})

    async def list_users(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        return web.json_response({"items": self.store.list_users()})

    async def create_user(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        user_id = self.store.create_user(
            str(payload.get("username", "")), str(payload.get("password", "")),
            str(payload.get("role", "operator")), bool(payload.get("must_change", True)),
        )
        self.store.audit(user.username, "user.create", "user", str(user_id))
        return web.json_response({"id": user_id}, status=201)

    async def list_tokens(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        return web.json_response({"items": self.store.list_api_tokens()})

    async def create_token(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        token_id, token = self.store.create_api_token(str(payload.get("name", "")), payload.get("scopes", []))
        self.store.audit(user.username, "token.create", "api_token", str(token_id))
        return web.json_response({"id": token_id, "token": token}, status=201)

    async def revoke_token(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        token_id = int(request.match_info["id"])
        self.store.revoke_api_token(token_id)
        self.store.audit(user.username, "token.revoke", "api_token", str(token_id))
        return web.json_response({"ok": True})

    async def cleanup(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        payload = await _json_body(request)
        execute = bool(payload.get("execute", False))
        result = self.archive.cleanup(int(payload.get("retention_days", self.config.retention_days)), execute)
        self.store.audit(user.username, "maintenance.cleanup" if execute else "maintenance.cleanup_preview", "system", detail={"removed": result["removed_count"]})
        return web.json_response(result)

    async def backup(self, request: web.Request) -> web.Response:
        user = _web_session(request); _require_admin(user)
        stamp = time.strftime("%Y%m%d_%H%M%S")
        destination = Path(self.config.data_dir) / "backups" / ("report-center_%s.sqlite3" % stamp)
        result = self.store.backup(str(destination))
        self.store.audit(user.username, "maintenance.backup", "system", detail={"size": result["size"]})
        return web.json_response(result)

    async def camera_capture(self, request: web.Request) -> web.Response:
        payload = await _json_body(request, max_bytes=5 * 1024 * 1024)
        result = await self.coordinator.camera_capture(payload)
        return web.json_response(result, status=201 if result["created"] else 200)

    async def internal_field_rules(self, request: web.Request) -> web.Response:
        active = self.store.active_profile_revision()
        return web.json_response(_fixed_field_rules_response(active))

    async def update_internal_field_rules(self, request: web.Request) -> web.Response:
        payload = await _json_body(request, max_bytes=128 * 1024)
        schema = _normalize_fixed_field_rules_payload(payload)
        active = self.store.active_profile_revision()
        updated = copy.deepcopy(active["config"])
        updated["field_resolver"] = _fixed_field_resolver(schema)
        _validate_profile(updated, self.store)
        revision_id = self.store.save_profile_draft(
            int(active["profile_id"]), updated, "camera-field-config"
        )
        self.store.publish_profile(
            int(active["profile_id"]), "camera-field-config", revision_id
        )
        current = self.store.active_profile_revision()
        write_runtime_template(self.config.camera_template_runtime_file, current)
        self.store.audit(
            "camera-web",
            "profile.fixed_field_rules_publish",
            "profile",
            str(active["profile_id"]),
            {"revision_id": revision_id, "field_count": len(schema["fields"])},
        )
        return web.json_response(_fixed_field_rules_response(current))

    async def internal_field_result(self, request: web.Request) -> web.Response:
        capture_id = str(request.query.get("capture_id", "")).strip()
        if not capture_id:
            raise ValidationError("capture_id is required")
        result = self.store.latest_camera_patient_result(capture_id)
        active = self.store.active_profile_revision()
        if result is not None and int(result.get("profile_revision_id") or 0) != int(active["id"]):
            return web.json_response({
                "available": False,
                "status": "waiting",
                "capture_id": capture_id,
                "message": "字段规则已更新，请取走当前报告，下一张生效",
            })
        return web.json_response(_fixed_field_result_response(capture_id, result))

    async def internal_reports(self, request: web.Request) -> web.Response:
        payload = await _json_body(request)
        paths = payload.get("paths", [])
        if isinstance(payload.get("path"), str):
            paths = [payload["path"]]
        if not isinstance(paths, list) or not paths:
            raise ValidationError("path or paths is required")
        reports = self.coordinator.receive_reports([str(path) for path in paths], str(payload.get("source", "")))
        self.upload_worker.wake()
        return web.json_response({"items": reports}, status=201)

    async def external_report(self, request: web.Request) -> web.Response:
        try:
            expected = Path(self.config.external_report_token_file).read_text(
                encoding="utf-8"
            ).strip()
        except OSError:
            LOGGER.error("external report token file is unavailable")
            return web.json_response({"error": "external report linking is unavailable"}, status=503)
        supplied = request.headers.get("X-Internal-Token", "")
        if not expected or not supplied or not secrets_compare(supplied, expected):
            return web.json_response({"error": "invalid internal token"}, status=403)
        payload = await _json_body(request)
        result = self.store.associate_external_report(
            int(payload.get("report_job_id", 0)),
            str(payload.get("pdf_sha256", "")),
            str(payload.get("source", "unknown")),
            float(payload.get("created_at", 0)),
            self.config.external_report_window_seconds,
        )
        return web.json_response(
            {
                "status": result["status"],
                "patient_session_id": result.get("patient_session_id") or "",
                "capture_id": result.get("capture_id") or "",
                "pdf_sha256": result["pdf_sha256"],
            }
        )

    async def archive_reports(self, request: web.Request) -> web.Response:
        _require_archive_token(request, self.store, "reports:read")
        result = self.store.list_reports(dict(request.query))
        result["items"] = [_public_report(item) for item in result["items"]]
        return web.json_response(result)

    async def archive_report(self, request: web.Request) -> web.Response:
        _require_archive_token(request, self.store, "reports:read")
        return web.json_response(_public_report(self.store.get_report(int(request.match_info["id"]))))

    async def archive_content(self, request: web.Request) -> web.StreamResponse:
        _require_archive_token(request, self.store, "reports:download")
        report = self.store.get_report(int(request.match_info["id"]))
        return _file_response(report, self.config.archive_dir, attachment=True)


def _validate_profile(config: Any, store: ReportCenterStore) -> list[str]:
    if not isinstance(config, dict):
        raise ValidationError("profile config must be an object")
    mode = str(config.get("patient_input_mode", ""))
    if mode not in PATIENT_INPUT_MODES:
        raise ValidationError("invalid patient_input_mode")
    source = str(config.get("report_source", ""))
    if source not in REPORT_SOURCES:
        raise ValidationError("report_source must be msc or printer")
    checks = ["patient input mode", "report source"]
    if bool(config.get("camera_intake_enabled", False)):
        if mode not in {"camera_query", "camera_direct"}:
            raise ValidationError("camera intake requires camera_query or camera_direct mode")
        checks.append("camera intake")
    connector_id = config.get("patient_connector_id")
    if mode in {"scanner_query", "camera_query"}:
        if not connector_id:
            raise ValidationError("query mode requires patient_connector_id")
        connector = store.get_connector(int(connector_id))
        if connector["type"] not in {"sql_proxy", "rest_json"}:
            raise ValidationError("patient connector has the wrong type")
        checks.append("patient connector")
    fields = _validate_field_resolver(config.get("field_resolver", {}))
    template = config.get("camera_template", {})
    if template:
        if not isinstance(template, dict):
            raise ValidationError("camera_template must be an object")
        if str(template.get("mode", "fixed_roi")) != "fixed_roi":
            raise ValidationError("camera template mode must be fixed_roi")
        if str(template.get("selection_mode", "manual")) != "manual":
            raise ValidationError("only manual camera template selection is supported")
        size = template.get("canonical_image_size", [])
        if size and (
            not isinstance(size, list) or len(size) != 2
            or any(int(value) < 1 for value in size)
        ):
            raise ValidationError("camera template image size is invalid")
        checks.append("camera fixed-region template")
    if bool(config.get("camera_patient_enabled", False)):
        if not any(bool(field.get("enabled", True)) for field in fields):
            raise ValidationError("camera patient processing requires at least one enabled OCR field")
        checks.append("camera patient fields")
    if mode == "camera_direct" and not fields:
        raise ValidationError("camera_direct requires at least one OCR field rule")
    upload_id = config.get("upload_target_id")
    if upload_id:
        target = store.get_connector(int(upload_id))
        if target["type"] not in {"report_multipart", "rest_multipart"}:
            raise ValidationError("upload target has the wrong type")
        checks.append("upload target")
    hid = config.get("hid", {})
    actions = hid.get("actions", []) if isinstance(hid, dict) else []
    allowed = {"click", "double_click", "input_field", "input_literal", "key", "hotkey", "wait", "condition", "wait_for_text"}
    if not isinstance(actions, list) or any(not isinstance(item, dict) or item.get("type") not in allowed for item in actions):
        raise ValidationError("HID actions contain an unsupported action")
    coordinate_mode = str(hid.get("coordinate_mode", "legacy")) if isinstance(hid, dict) else "legacy"
    if coordinate_mode not in {"legacy", "fixed_absolute"}:
        raise ValidationError("unsupported HID coordinate mode")
    if coordinate_mode == "fixed_absolute":
        basis = hid.get("coordinate_basis", {})
        if not isinstance(basis, dict):
            raise ValidationError("fixed HID coordinate basis must be an object")
        try:
            width = int(basis.get("width", 0))
            height = int(basis.get("height", 0))
        except (TypeError, ValueError):
            raise ValidationError("fixed HID coordinate basis is invalid") from None
        if width < 1 or height < 1:
            raise ValidationError("fixed HID coordinate basis is invalid")
        if bool(config.get("auto_entry_enabled", False)) and not actions:
            raise ValidationError("fixed HID auto entry requires at least one action")
        _validate_fixed_hid_actions(actions, width, height)
        checks.append("fixed absolute HID actions")
    else:
        checks.append("HID actions")
    return checks


def _validate_fixed_hid_actions(actions: list[dict[str, Any]], width: int, height: int) -> None:
    allowed = {"click", "double_click", "input_field", "input_literal", "key", "hotkey", "wait", "condition"}
    coordinate_actions = {"click", "double_click", "input_field", "input_literal"}
    for action in actions:
        action_type = str(action.get("type", "")).strip()
        if action_type not in allowed:
            raise ValidationError("fixed HID actions cannot use screen recognition")
        if action_type in coordinate_actions:
            _validate_fixed_hid_point(action, width, height)
        if action_type == "input_field" and not str(action.get("field", "")).strip():
            raise ValidationError("fixed HID input_field requires a patient field")
        if action_type == "condition":
            if not str(action.get("field", "")).strip():
                raise ValidationError("fixed HID condition requires a patient field")
            nested = action.get("action")
            if isinstance(nested, dict):
                _validate_fixed_hid_actions([nested], width, height)
            elif "x" in action and "y" in action:
                _validate_fixed_hid_point(action, width, height)
            else:
                raise ValidationError("fixed HID condition requires an action or click point")


def _validate_fixed_hid_point(action: dict[str, Any], width: int, height: int) -> None:
    try:
        x = int(action["x"])
        y = int(action["y"])
    except (KeyError, TypeError, ValueError):
        raise ValidationError("fixed HID action requires integer x and y") from None
    if x < 0 or x >= width or y < 0 or y >= height:
        raise ValidationError("fixed HID action coordinate is outside the configured screen")


def _validate_field_resolver(field_resolver: Any) -> list[dict[str, Any]]:
    if not isinstance(field_resolver, dict):
        raise ValidationError("field_resolver must be an object")
    provider = str(field_resolver.get("provider", "rules"))
    if provider == "model":
        raise ValidationError("model field resolver provider is not installed")
    if provider != "rules":
        raise ValidationError("unsupported field resolver provider")
    matching_mode = str(field_resolver.get("matching_mode", "flexible"))
    if matching_mode not in {"flexible", "fixed_label"}:
        raise ValidationError("unsupported field resolver matching mode")
    fields = field_resolver.get("fields", [])
    if not isinstance(fields, list):
        raise ValidationError("field resolver fields must be an array")
    import re
    for field in fields:
        if not isinstance(field, dict) or not str(field.get("field_key", "")).strip():
            raise ValidationError("each OCR field requires field_key")
        pattern = str(field.get("regex", ""))
        if pattern:
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ValidationError("OCR field regex is invalid: %s" % exc) from exc
        roi = field.get("roi")
        if roi is not None and not valid_normalized_roi(roi):
            raise ValidationError("OCR field ROI must be ordered coordinates in range 0..1000")
        match_mode = str(field.get("match_mode", "label_assisted"))
        if match_mode not in {"label_assisted", "fixed_roi", "format_only"}:
            raise ValidationError("unsupported OCR field match mode")
        if match_mode == "fixed_roi" and not valid_normalized_roi(roi):
            raise ValidationError("fixed-region OCR field requires a valid ROI")
        if str(field.get("join_mode", "single")) not in {"single", "reading_order"}:
            raise ValidationError("unsupported OCR field join mode")
        labels = [str(value).strip() for value in field.get("label_aliases", []) if str(value).strip()]
        if len(labels) > 8 or any(len(value) > 80 for value in labels):
            raise ValidationError("OCR field labels are invalid")
        relations = field.get("relations", [])
        allowed_relations = {"same_text", "same_line_right", "next_line_same_column", "nearest"}
        if relations and (
            not isinstance(relations, list)
            or any(str(value) not in allowed_relations for value in relations)
        ):
            raise ValidationError("OCR field relations are invalid")
        if matching_mode == "fixed_label":
            if len(labels) != 1:
                raise ValidationError("fixed-label fields require exactly one label")
            if not relations or "nearest" in {str(value) for value in relations}:
                raise ValidationError("fixed-label fields only support same-text, right or below relations")
        try:
            minimum_score = float(field.get("min_ocr_score", 0.0))
            maximum_distance = float(field.get("max_distance", 0.25))
        except (TypeError, ValueError):
            raise ValidationError("OCR field score or distance is invalid") from None
        if not 0 <= minimum_score <= 1 or not 0 < maximum_distance <= 1:
            raise ValidationError("OCR field score or distance is invalid")
        if str(field.get("char_type", "any")) not in {"any", "digits", "alnum"}:
            raise ValidationError("OCR field character type is invalid")
    return fields


def _normalize_fixed_field_rules_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or set(payload) != {"fields"}:
        raise ValidationError("field rule payload must contain only fields")
    raw_fields = payload.get("fields")
    if not isinstance(raw_fields, list) or not raw_fields or len(raw_fields) > len(STANDARD_PATIENT_FIELDS):
        raise ValidationError("field rules must contain 1 to 14 entries")
    allowed_keys = {
        "field_key", "label", "position", "char_type", "fixed_length",
        "min_ocr_score", "max_distance", "required",
    }
    normalized = []
    field_keys: set[str] = set()
    labels: set[str] = set()
    for raw in raw_fields:
        if not isinstance(raw, dict) or set(raw) - allowed_keys:
            raise ValidationError("field rule contains unsupported settings")
        field_key = str(raw.get("field_key", "")).strip()
        label = str(raw.get("label", "")).strip()
        position = str(raw.get("position", "right_then_below")).strip()
        char_type = str(raw.get("char_type", "any")).strip()
        if field_key not in STANDARD_PATIENT_FIELDS or field_key in field_keys:
            raise ValidationError("field key must be a unique standard patient field")
        if not label or len(label) > 80 or label in labels:
            raise ValidationError("each field requires a unique fixed label")
        if position not in {"right", "below", "right_then_below"}:
            raise ValidationError("field position must be right, below or right_then_below")
        if char_type not in {"any", "digits", "alnum"}:
            raise ValidationError("field character type is invalid")
        try:
            fixed_length = int(raw.get("fixed_length", 0) or 0)
            minimum_score = float(raw.get("min_ocr_score", 0.65))
            maximum_distance = int(raw.get("max_distance", 180))
        except (TypeError, ValueError):
            raise ValidationError("field length, score or distance is invalid") from None
        if not 0 <= fixed_length <= 128:
            raise ValidationError("fixed field length must be 0 to 128")
        if not 0 <= minimum_score <= 1:
            raise ValidationError("minimum OCR score must be 0 to 1")
        if not 10 <= maximum_distance <= 500:
            raise ValidationError("maximum label distance must be 10 to 500")
        if not isinstance(raw.get("required", False), bool):
            raise ValidationError("required must be boolean")
        field_keys.add(field_key)
        labels.add(label)
        normalized.append({
            "field_key": field_key,
            "label": label,
            "position": position,
            "char_type": char_type,
            "fixed_length": fixed_length,
            "min_ocr_score": round(minimum_score, 4),
            "max_distance": maximum_distance,
            "required": bool(raw.get("required", False)),
        })
    return {"fields": normalized}


def _fixed_field_resolver(schema: dict[str, Any]) -> dict[str, Any]:
    relation_map = {
        "right": ["same_text", "same_line_right"],
        "below": ["same_text", "next_line_same_column"],
        "right_then_below": ["same_text", "same_line_right", "next_line_same_column"],
    }
    fields = []
    for field in schema["fields"]:
        fixed_length = int(field["fixed_length"])
        fields.append({
            "field_key": field["field_key"],
            "target": field["field_key"],
            "enabled": True,
            "required": bool(field["required"]),
            "match_mode": "label_assisted",
            "label_aliases": [field["label"]],
            "relations": relation_map[field["position"]],
            "char_type": field["char_type"],
            "lengths": [fixed_length] if fixed_length else [],
            "min_length": fixed_length or 1,
            "max_length": fixed_length or 512,
            "min_ocr_score": float(field["min_ocr_score"]),
            "max_distance": int(field["max_distance"]) / 1000.0,
            "regex": "",
            "roi": None,
        })
    return {"provider": "rules", "matching_mode": "fixed_label", "fields": fields}


def _fixed_field_rules_response(active: dict[str, Any]) -> dict[str, Any]:
    resolver = active.get("config", {}).get("field_resolver", {})
    fields = []
    for definition in resolver.get("fields", []) if isinstance(resolver, dict) else []:
        if not isinstance(definition, dict) or not bool(definition.get("enabled", True)):
            continue
        labels = [str(value).strip() for value in definition.get("label_aliases", []) if str(value).strip()]
        if not labels:
            continue
        relations = {str(value) for value in definition.get("relations", [])}
        if "same_line_right" in relations and "next_line_same_column" in relations:
            position = "right_then_below"
        elif "next_line_same_column" in relations:
            position = "below"
        else:
            position = "right"
        lengths = [int(value) for value in definition.get("lengths", []) if int(value) > 0]
        fields.append({
            "field_key": str(definition.get("field_key", "")),
            "label": labels[0],
            "position": position,
            "char_type": str(definition.get("char_type", "any")),
            "fixed_length": lengths[0] if len(lengths) == 1 else 0,
            "min_ocr_score": round(float(definition.get("min_ocr_score", 0.65)), 4),
            "max_distance": int(round(float(definition.get("max_distance", 0.18)) * 1000)),
            "required": bool(definition.get("required", False)),
        })
    return {
        "available": True,
        "schema": {
            "schema_version": 1,
            "engine": "fixed_label_rules",
            "profile_version": int(active.get("version", 0)),
            "fields": fields,
        },
    }


def _fixed_field_result_response(
    capture_id: str, result: Optional[dict[str, Any]]
) -> dict[str, Any]:
    if result is None:
        return {
            "available": False,
            "status": "waiting",
            "capture_id": capture_id,
            "message": "等待当前报告的固定字段结果",
        }
    evidence = result.get("evidence", {})
    fields = {}
    if isinstance(evidence, dict):
        for key, item in evidence.items():
            if key not in STANDARD_PATIENT_FIELDS or not isinstance(item, dict):
                continue
            fields[key] = {
                "value": str(item.get("value", ""))[:512],
                "probability": round(float(item.get("score", 0.0)), 4),
                "source_span_ids": [int(value) for value in item.get("span_ids", [])[:32]],
                "fixed_label": str(item.get("label", ""))[:80],
                "relation": str(item.get("relation", ""))[:48],
            }
    review_fields = sorted(set(result.get("missing_fields", [])) | set(result.get("conflict_fields", [])))
    response = result.get("response", {})
    if not isinstance(response, dict):
        response = {"code": "ERROR", "data": [], "msg": "患者信息结果无效", "success": False}
    return {
        "available": True,
        "status": str(result.get("status", "error")),
        "capture_id": capture_id,
        "engine": "fixed_label_rules",
        "fields": fields,
        "review_fields": [value for value in review_fields if value in STANDARD_PATIENT_FIELDS],
        "patient_json": response,
        "timings": {},
    }


def _validate_connector_definition(connector_type: str, config: Any) -> None:
    if connector_type not in {"sql_proxy", "rest_json", "report_multipart", "rest_multipart"}:
        raise ValidationError("unsupported connector type")
    if not isinstance(config, dict):
        raise ValidationError("connector config must be an object")
    endpoint = str(config.get("endpoint", ""))
    if not endpoint.startswith(("http://", "https://")):
        raise ValidationError("connector endpoint must be HTTP or HTTPS")
    headers = config.get("headers", {})
    if not isinstance(headers, dict):
        raise ValidationError("connector headers must be an object")
    if connector_type == "sql_proxy":
        render_sql_template(str(config.get("sql_template", "")), "test")
    if connector_type == "rest_json" and str(config.get("method", "POST")).upper() not in {"GET", "POST"}:
        raise ValidationError("REST patient connector only supports GET or POST")
    if connector_type in {"report_multipart", "rest_multipart"}:
        correction = str(config.get("correction_mode", "local_only"))
        if correction not in {"local_only", "reupload"}:
            raise ValidationError("correction_mode must be local_only or reupload")


async def _json_body(request: web.Request, max_bytes: int = 1024 * 1024) -> dict[str, Any]:
    if request.content_length is not None and request.content_length > max_bytes:
        raise web.HTTPRequestEntityTooLarge(max_size=max_bytes, actual_size=request.content_length)
    try:
        payload = await request.json()
    except Exception:
        raise ValidationError("request JSON is invalid") from None
    if not isinstance(payload, dict):
        raise ValidationError("request body must be an object")
    return payload


def _file_response(report: dict[str, Any], archive_root: str, attachment: bool) -> web.FileResponse:
    if report.get("status") == "purged":
        raise web.HTTPGone(text="report content was purged")
    path = Path(str(report.get("archive_path", ""))).resolve()
    root = Path(archive_root).resolve()
    try:
        inside = os.path.commonpath([str(path), str(root)]) == str(root)
    except ValueError:
        inside = False
    if not inside or not path.is_file():
        raise NotFoundError("report PDF is unavailable")
    disposition = "attachment" if attachment else "inline"
    response = web.FileResponse(path, headers={
        "Content-Disposition": "%s; filename*=UTF-8''%s" % (disposition, _quote_filename(path.name)),
        "ETag": str(report["sha256"]),
        "Content-Length": str(path.stat().st_size),
    })
    return response


def _require_archive_token(request: web.Request, store: ReportCenterStore, scope: str) -> None:
    authorization = request.headers.get("Authorization", "")
    if not authorization.startswith("Bearer ") or not store.verify_api_token(authorization[7:].strip(), scope):
        raise web.HTTPUnauthorized(headers={"WWW-Authenticate": "Bearer"})


def _web_session(request: web.Request) -> WebSession:
    session = request.get("session")
    if not isinstance(session, WebSession):
        raise web.HTTPUnauthorized()
    return session


def _require_admin(session: WebSession) -> None:
    if session.role != "admin":
        raise web.HTTPForbidden(text="administrator permission required")


def _session_payload(session: WebSession) -> dict[str, Any]:
    return {"ok": True, "csrf": session.csrf, "username": session.username, "role": session.role, "must_change": session.must_change}


def _is_loopback(remote: Optional[str]) -> bool:
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


def _ssl_context(config: ReportCenterConfig) -> Optional[ssl.SSLContext]:
    if not config.ssl_cert or not config.ssl_key:
        return None
    context = ssl.create_default_context(ssl.Purpose.CLIENT_AUTH)
    context.load_cert_chain(config.ssl_cert, config.ssl_key)
    return context


def _quote_filename(value: str) -> str:
    from urllib.parse import quote
    return quote(value, safe="")


def _public_report(report: dict[str, Any]) -> dict[str, Any]:
    result = dict(report)
    result.pop("archive_path", None)
    result.pop("temp_path", None)
    return result


def _camera_patient_response(result: Optional[dict[str, Any]]) -> dict[str, Any]:
    if result is None:
        return {"code": "NOT_READY", "data": [], "msg": "尚未生成患者信息", "success": False}
    response = result.get("response", {})
    if not isinstance(response, dict):
        return {"code": "ERROR", "data": [], "msg": "患者信息结果无效", "success": False}
    return response


def secrets_compare(left: str, right: str) -> bool:
    import hmac
    return hmac.compare_digest(left.encode("utf-8"), right.encode("utf-8"))
