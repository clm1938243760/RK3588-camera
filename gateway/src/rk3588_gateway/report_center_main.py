from __future__ import annotations

import argparse
import asyncio
import logging
import os
import signal
import subprocess
from dataclasses import replace
from pathlib import Path
from typing import Any

from .config import load_config
from .events import GatewayEvent
from .gpio import GpioController
from .hid import ScannerReader
from .main import ensure_device_profile
from .msc_monitor import MscMonitor
from .print_capture import PrintCapture
from .printer import Printer
from .queue import EventQueue
from .report_center.archive import ReportArchive
from .report_center.camera_runtime import write_runtime_template
from .report_center.config import default_profile_snapshot, load_report_center_config
from .report_center.connectors import PatientConnectorService
from .report_center.coordinator import ReportCenterCoordinator
from .report_center.domain import ConflictError
from .report_center.store import ReportCenterStore
from .report_center.upload import ReportCenterUploadWorker
from .report_center.web import ReportCenterWeb
from .report_pdf import ReportPdfConverter
from .vm_transfer import VmTransfer
from .workflow import GatewayWorkflow


LOGGER = logging.getLogger(__name__)

LEGACY_SQL_TEMPLATE = """select
  t.exam_item as exam_item,
  t.his_exam_no,
  z.report_no,
  t.patient_id,
  t.patient_name,
  q.name_phonetic,
  substr(t.patient_name, 0, 2) as xing,
  substr(t.patient_name, 2, 8) as ming,
  t.sex,
  t.age,
  to_char(t.birthday,'yyyy') as nian,
  to_char(t.birthday,'mm') as yue,
  to_char(t.birthday,'dd') as ri,
  t.birthday
from exam_master t
left join exam_item z on t.his_exam_no=z.his_exam_no
left join patient_info q on t.patient_id=q.patient_id
where (
  z.report_no like {{query_like}}
  or t.patient_id like {{query_like}}
  or t.patient_name like {{query_like}}
)
and z.exam_state in ('10', '20', '30', '40')
and t.req_date>= CURRENT_DATE - INTERVAL '180 days'
order by t.req_date desc
limit 20"""


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="RK3588 patient intake and report center")
    parser.add_argument("--config", default="config.yaml")
    return parser.parse_args()


async def main_async() -> None:
    args = parse_args()
    config_path = Path(args.config)
    app_config = load_config(config_path)
    center_config = load_report_center_config(config_path)
    logging.basicConfig(
        level=getattr(logging, app_config.logging.level, logging.INFO),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    ensure_device_profile(app_config)
    if not center_config.enabled:
        LOGGER.warning("report center is disabled in configuration")
        return
    center_config = _ensure_tls(center_config, app_config.device.id)
    store = ReportCenterStore(center_config.database_path)
    generated_password = store.bootstrap_admin(center_config.bootstrap_admin_password)
    if generated_password:
        password_file = Path(center_config.data_dir) / "bootstrap-admin-password"
        password_file.parent.mkdir(parents=True, exist_ok=True)
        password_file.write_text(generated_password + "\n", encoding="utf-8")
        try:
            os.chmod(password_file, 0o600)
        except OSError:
            pass
        LOGGER.warning("initial admin password written to %s; change it after first login", password_file)

    connector_id = _ensure_legacy_patient_connector(store, app_config)
    snapshot = default_profile_snapshot(app_config)
    snapshot["patient_connector_id"] = connector_id
    store.ensure_default_profile(snapshot)
    write_runtime_template(
        center_config.camera_template_runtime_file,
        store.active_profile_revision(),
    )

    queue = EventQueue(app_config.storage.sqlite_path)
    workflow = GatewayWorkflow(app_config, queue)
    archive = ReportArchive(center_config, store)
    connectors = PatientConnectorService()
    coordinator = ReportCenterCoordinator(
        store, archive, connectors, center_config.shadow_mode,
        entry_handler=workflow.execute_patient_entry,
        intake_only=center_config.intake_only,
        hid_active_marker=center_config.hid_active_marker,
        display_state_path=app_config.local_api.display_state_path,
        camera_configuration_image_dir=center_config.camera_configuration_image_dir,
        template_image_dir=center_config.template_image_dir,
        entry_capture_dir=center_config.entry_capture_dir,
    )
    upload_worker = ReportCenterUploadWorker(
        center_config,
        store,
        app_config.local_api.display_state_path,
    )

    async def hardware_status() -> dict[str, Any]:
        return {
            "mode": "shadow" if center_config.shadow_mode else "active",
            "scanner": {"enabled": app_config.scanner.enabled and not center_config.intake_only, "device": Path(app_config.scanner.event_device).exists()},
            "hid": {
                "enabled": app_config.hid_input.enabled,
                "keyboard": Path(app_config.hid_input.keyboard_device).exists(),
                "mouse": Path(app_config.hid_input.mouse_device).exists(),
                "active": workflow.is_hid_input_active(),
            },
            "printer_capture": {"enabled": False if center_config.intake_only else app_config.print_capture.enabled, "managed_by": "gadget-collector" if center_config.intake_only else "report-center", "device": Path(app_config.print_capture.device).exists()},
            "msc": {"enabled": False if center_config.intake_only else app_config.msc.enabled, "managed_by": "gadget-collector" if center_config.intake_only else "report-center", "image": Path(app_config.msc.image_path).exists()},
            "camera": {"managed_by": "rk3588-camera", "internal_endpoint": "/internal/v1/camera-captures"},
            "ocr": {"managed_by": "rk3588-ppocr", "changed": False},
        }

    web_server = ReportCenterWeb(
        center_config, store, coordinator, archive, connectors, upload_worker, hardware_status
    )
    stop_event = asyncio.Event()
    scanner = None
    print_capture = None
    msc_monitor = None
    gpio = None
    tasks: list[asyncio.Task[Any]] = []

    def request_stop() -> None:
        if scanner:
            scanner.stop()
        if print_capture:
            print_capture.stop()
        if msc_monitor:
            msc_monitor.stop()
        coordinator.stop()
        upload_worker.stop()
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        loop.add_signal_handler(sig, request_stop)

    await web_server.start()
    tasks.append(asyncio.create_task(coordinator.run()))
    if not center_config.intake_only:
        tasks.append(asyncio.create_task(_report_event_bridge(
            queue, coordinator, stop_event, destructive=not center_config.shadow_mode,
            baseline_existing=center_config.shadow_mode,
        )))

    if center_config.shadow_mode:
        LOGGER.warning("report center is in shadow mode; HID, scanner, MSC, printer capture and upload are not owned")
    elif center_config.intake_only:
        LOGGER.info("report center intake-only mode: camera and HID enabled; scanner/report capture/upload disabled")
    else:
        printer = Printer(app_config.printer)
        gpio = GpioController(app_config.gpio)
        report_pdf = ReportPdfConverter(app_config.report_pdf)
        vm_transfer = VmTransfer(app_config.vm_transfer)
        print_capture = PrintCapture(
            app_config.print_capture, queue, app_config.device.id, vm_transfer, report_pdf, None
        )
        scanner = ScannerReader(app_config.scanner, app_config.device.id)

        def before_gadget_unbind() -> bool:
            return workflow.hid_output.close_usb_gadget_fds("report center MSC unbind") and print_capture.pause_for_gadget_unbind()

        def after_gadget_rebind() -> None:
            workflow.hid_output.close_usb_gadget_fds("report center MSC rebind")
            print_capture.resume_after_gadget_rebind()

        msc_monitor = MscMonitor(
            app_config.msc, queue, app_config.device.id, report_pdf, None,
            workflow.is_hid_input_active, before_gadget_unbind, after_gadget_rebind,
        )

        async def scanner_event(event: GatewayEvent) -> None:
            code = str(event.payload.get("code", "")).strip()
            if code:
                try:
                    await coordinator.intake({"mode": "scanner_query", "code": code}, source="scanner")
                except Exception:
                    LOGGER.exception("scanner intake failed ref=%s", event.id[:12])

        await gpio.start()
        tasks.extend([
            asyncio.create_task(scanner.run(scanner_event)),
            asyncio.create_task(print_capture.run()),
            asyncio.create_task(msc_monitor.run()),
            asyncio.create_task(upload_worker.run()),
        ])

    await stop_event.wait()
    await web_server.stop()
    if gpio:
        await gpio.stop()
    for task in tasks:
        task.cancel()
    await asyncio.gather(*tasks, return_exceptions=True)


async def _report_event_bridge(
    queue: EventQueue,
    coordinator: ReportCenterCoordinator,
    stop_event: asyncio.Event,
    destructive: bool,
    baseline_existing: bool,
) -> None:
    seen: set[str] = set()
    first_cycle = True
    while not stop_event.is_set():
        events = list(reversed(queue.list_recent(200)))
        if first_cycle and baseline_existing:
            seen.update(str(event.get("id", "")) for event in events)
            first_cycle = False
            await asyncio.sleep(1.0)
            continue
        first_cycle = False
        if not destructive:
            for event in events:
                if event.get("type") != "barcode.scan" or event.get("id") in seen:
                    continue
                code = str((event.get("payload") or {}).get("code", "")).strip()
                try:
                    if code:
                        await coordinator.intake({"mode": "scanner_query", "code": code}, source="shadow_scanner")
                    seen.add(str(event["id"]))
                except Exception:
                    LOGGER.exception("shadow scanner event failed ref=%s", str(event.get("id", ""))[:12])
        groups: dict[str, list[dict[str, Any]]] = {}
        for event in events:
            if event.get("type") != "report.pdf_created" or event.get("id") in seen:
                continue
            payload = event.get("payload", {})
            source = str(payload.get("source_type", ""))
            path = str(payload.get("path", ""))
            if source in {"msc", "print", "printer"} and path:
                groups.setdefault("printer" if source == "print" else source, []).append(event)
        for source, selected in groups.items():
            ids = [str(event["id"]) for event in selected]
            paths = [str(event["payload"]["path"]) for event in selected]
            try:
                coordinator.receive_reports(paths, source)
                if destructive:
                    queue.mark_sent(ids)
                seen.update(ids)
            except ConflictError:
                seen.update(ids)
                LOGGER.info("report event already handled ids=%s", ",".join(value[:8] for value in ids))
            except Exception:
                LOGGER.exception("report event bridge failed source=%s count=%d", source, len(paths))
        if len(seen) > 5000:
            seen = set(list(seen)[-2000:])
        await asyncio.sleep(1.0)


def _ensure_legacy_patient_connector(store: ReportCenterStore, app_config: Any) -> int:
    existing = [item for item in store.list_connectors() if item["type"] in {"sql_proxy", "rest_json"}]
    if existing:
        return int(existing[0]["id"])
    return store.create_connector(
        "Legacy patient SQL proxy",
        "sql_proxy",
        {
            "endpoint": app_config.patient_api.endpoint,
            "timeout_seconds": app_config.patient_api.timeout_seconds,
            "headers": {"User-Agent": app_config.patient_api.user_agent},
            "request_field": "sqlStr",
            "sql_template": LEGACY_SQL_TEMPLATE,
            "records_path": "$.data",
            "field_mapping": {},
        },
    )


def _ensure_tls(config: Any, common_name: str) -> Any:
    if not config.ssl_cert or not config.ssl_key:
        LOGGER.warning("report center HTTPS is explicitly disabled")
        return config
    cert = Path(config.ssl_cert)
    key = Path(config.ssl_key)
    if cert.is_file() and key.is_file():
        return config
    cert.parent.mkdir(parents=True, exist_ok=True)
    key.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        [
            "openssl", "req", "-x509", "-newkey", "rsa:2048", "-sha256", "-nodes",
            "-keyout", str(key), "-out", str(cert), "-days", "3650",
            "-subj", "/CN=%s" % common_name.replace("/", "_"),
        ],
        check=True,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    os.chmod(key, 0o600)
    os.chmod(cert, 0o644)
    return replace(config, ssl_cert=str(cert), ssl_key=str(key))


def main() -> None:
    asyncio.run(main_async())


if __name__ == "__main__":
    main()
