from __future__ import annotations

import logging
import ipaddress
from pathlib import Path
from typing import Optional

from aiohttp import web

from .config import AppConfig
from .display_state import read_display_state
from .display import html_response
from .events import GatewayEvent
from .gpio import GpioController
from .patient_api import PatientApiClient, PatientApiError, failed_patient_payload
from .printer import Printer
from .queue import EventQueue

LOGGER = logging.getLogger(__name__)


class LocalApi:
    def __init__(
        self,
        config: AppConfig,
        queue: EventQueue,
        printer: Printer,
        gpio: Optional[GpioController] = None,
    ) -> None:
        self.config = config
        self.queue = queue
        self.printer = printer
        self.gpio = gpio
        self.workflow = None
        self.app = web.Application()
        self.app.add_routes(
            [
                web.get("/health", self.health),
                web.get("/status", self.status),
                web.get("/events", self.list_events),
                web.post("/events", self.create_event),
                web.post("/scan", self.scan),
                web.post("/patient/query", self.patient_query),
                web.post("/print", self.print_text),
                web.get("/display", self.display_page),
                web.get("/display/state", self.display_state),
                web.get("/gpio", self.gpio_status),
                web.get("/gpio/{name}", self.gpio_read),
                web.post("/gpio/{name}", self.gpio_write),
                web.post("/gpio/{name}/pulse", self.gpio_pulse),
            ]
        )
        self.runner: Optional[web.AppRunner] = None

    async def start(self) -> None:
        if not self.config.local_api.enabled:
            LOGGER.info("local api disabled")
            return
        self.runner = web.AppRunner(self.app, access_log=None)
        await self.runner.setup()
        site = web.TCPSite(
            self.runner,
            self.config.local_api.host,
            self.config.local_api.port,
        )
        await site.start()
        LOGGER.info("local api listening on %s:%s", self.config.local_api.host, self.config.local_api.port)

    async def stop(self) -> None:
        if self.runner:
            await self.runner.cleanup()

    async def health(self, request: web.Request) -> web.Response:
        return web.json_response({"ok": True})

    async def status(self, request: web.Request) -> web.Response:
        return web.json_response(
            {
                "device_id": self.config.device.id,
                "location": self.config.device.location,
                "queued_events": self.queue.count(),
            }
        )

    async def list_events(self, request: web.Request) -> web.Response:
        limit = min(int(request.query.get("limit", "20")), 100)
        return web.json_response({"events": self.queue.list_recent(limit)})

    async def create_event(self, request: web.Request) -> web.Response:
        payload = await request.json()
        event = GatewayEvent(
            type=str(payload.get("type", "manual.event")),
            device_id=self.config.device.id,
            payload=dict(payload.get("payload", {})),
        )
        self.queue.put(event)
        return web.json_response({"queued": True, "id": event.id})

    async def scan(self, request: web.Request) -> web.Response:
        payload = await request.json()
        code = str(payload.get("code", "")).strip()
        if not code:
            return web.json_response({"ok": False, "error": "missing code"}, status=400)
        event = GatewayEvent(
            type="barcode.scan",
            device_id=self.config.device.id,
            payload={"code": code, "source": "local_api"},
        )
        self.queue.put(event)
        if self.workflow:
            try:
                self.workflow.start_scan(code)
            except Exception as exc:
                LOGGER.exception("scan workflow start failed code=%s", code)
                return web.json_response({"ok": False, "id": event.id, "error": str(exc)}, status=500)
        return web.json_response({"ok": True, "id": event.id})

    async def patient_query(self, request: web.Request) -> web.Response:
        if not _is_loopback(request.remote):
            return web.json_response(failed_patient_payload("仅允许本机查询"), status=403)
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(failed_patient_payload("请求 JSON 无效"), status=400)
        code = str(payload.get("code", "")).strip() if isinstance(payload, dict) else ""
        if not _valid_patient_query_code(code):
            return web.json_response(failed_patient_payload("查询号码格式无效"), status=400)
        try:
            response = await PatientApiClient(self.config.patient_api).query_payload(
                code,
                persist_raw=False,
            )
        except PatientApiError as exc:
            LOGGER.warning("local patient query failed reason=%s", str(exc))
            return web.json_response(failed_patient_payload(), status=502)
        return web.json_response(response)

    async def print_text(self, request: web.Request) -> web.Response:
        payload = await request.json()
        ok = await self.printer.print_text(
            text=str(payload.get("text", "")),
            title=str(payload.get("title", "rk3588-gateway")),
        )
        return web.json_response({"printed": ok})

    async def display_page(self, request: web.Request) -> web.Response:
        return web.Response(text=html_response(), content_type="text/html")

    async def display_state(self, request: web.Request) -> web.Response:
        events = self.queue.list_recent(30)
        shared_display = read_display_state(
            str(getattr(self.config.local_api, "display_state_path", "") or "")
        )
        display = shared_display or (self.workflow.get_display_state() if self.workflow else {})
        for event in events:
            event_type = event.get("type")
            if event_type in {"report.uploading", "report.uploaded", "report.upload_failed"} and self.workflow:
                payload = event.get("payload") if isinstance(event.get("payload"), dict) else {}
                changed = self.workflow.handle_report_upload(
                    str(event_type),
                    str(payload.get("path", "")),
                    str(payload.get("error", "")),
                    bool(payload.get("printed", False)),
                    str(event.get("created_at", "")),
                    str(event.get("id", "")),
                )
                if changed:
                    display = self.workflow.get_display_state()
                    break
        gpio = {"enabled": False, "lines": []}
        if self.gpio:
            await self.gpio.refresh_inputs()
            gpio = {"enabled": self.config.gpio.enabled, "lines": self.gpio.snapshot()}
        return web.json_response(
            {
                "display": display,
                "device_id": self.config.device.id,
                "location": self.config.device.location,
                "queued_events": self.queue.count(),
                "print_jobs": self._count_files(self.config.print_capture.output_dir),
                "msc_files": self._count_files(self.config.msc.output_dir),
                "last_scan": self._last_scan(events),
                "gpio": gpio,
                "events": self._compact_events(events[:6]),
            }
        )

    async def gpio_status(self, request: web.Request) -> web.Response:
        if not self.gpio:
            return web.json_response({"enabled": False, "lines": []})
        await self.gpio.refresh_inputs()
        return web.json_response({"enabled": self.config.gpio.enabled, "lines": self.gpio.snapshot()})

    async def gpio_read(self, request: web.Request) -> web.Response:
        if not self.gpio:
            return web.json_response({"ok": False, "error": "gpio disabled"}, status=404)
        name = request.match_info["name"]
        try:
            value = await self.gpio.read_value(name)
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "name": name, "value": value})

    async def gpio_write(self, request: web.Request) -> web.Response:
        if not self.gpio:
            return web.json_response({"ok": False, "error": "gpio disabled"}, status=404)
        name = request.match_info["name"]
        payload = await request.json()
        try:
            value = await self.gpio.set_value(name, int(payload.get("value", 0)))
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "name": name, "value": value})

    async def gpio_pulse(self, request: web.Request) -> web.Response:
        if not self.gpio:
            return web.json_response({"ok": False, "error": "gpio disabled"}, status=404)
        name = request.match_info["name"]
        payload = await request.json()
        try:
            value = await self.gpio.pulse(
                name,
                value=int(payload.get("value", 1)),
                duration_ms=int(payload.get("duration_ms", 200)),
            )
        except Exception as exc:
            return web.json_response({"ok": False, "error": str(exc)}, status=400)
        return web.json_response({"ok": True, "name": name, "value": value})

    def _count_files(self, directory: str) -> int:
        root = Path(directory)
        if not root.exists():
            return 0
        return sum(1 for path in root.rglob("*") if path.is_file())

    def _last_scan(self, events: list[dict[str, object]]) -> str:
        for event in events:
            if event.get("type") == "barcode.scan":
                payload = event.get("payload")
                if isinstance(payload, dict):
                    return str(payload.get("code", ""))
        return ""

    def _compact_events(self, events: list[dict[str, object]]) -> list[dict[str, object]]:
        compact: list[dict[str, object]] = []
        for event in events:
            payload = event.get("payload")
            small_payload = {}
            if isinstance(payload, dict):
                for key in ("code", "path", "bytes"):
                    if key in payload:
                        small_payload[key] = payload[key]
            compact.append(
                {
                    "id": event.get("id", ""),
                    "type": event.get("type", ""),
                    "created_at": event.get("created_at", ""),
                    "payload": small_payload,
                }
            )
        return compact


def _is_loopback(remote: Optional[str]) -> bool:
    if not remote:
        return False
    try:
        return ipaddress.ip_address(remote).is_loopback
    except ValueError:
        return False


def _valid_patient_query_code(code: str) -> bool:
    return 4 <= len(code) <= 64 and all(
        char.isascii() and (char.isalnum() or char in "_-")
        for char in code
    )
