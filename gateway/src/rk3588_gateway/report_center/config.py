from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

import yaml


@dataclass(frozen=True)
class ReportCenterConfig:
    enabled: bool
    shadow_mode: bool
    intake_only: bool
    data_dir: str
    database_path: str
    archive_dir: str
    incoming_dir: str
    host: str
    port: int
    ssl_cert: str
    ssl_key: str
    session_hours: int
    retention_days: int
    report_info_path: str
    upload_poll_seconds: float
    upload_retry_seconds: int
    upload_max_attempts: int
    camera_loopback_only: bool
    bootstrap_admin_password: str
    portal_dir: str
    camera_configuration_image_dir: str
    template_image_dir: str
    camera_template_runtime_file: str
    camera_full_page_once_file: str
    hid_active_marker: str
    external_report_token_file: str
    external_report_window_seconds: int
    entry_capture_dir: str = ""


def load_report_center_config(path: Union[str, Path]) -> ReportCenterConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    section = raw.get("report_center", {})
    if not isinstance(section, dict):
        raise ValueError("invalid config section: report_center")

    data_dir = str(section.get("data_dir", "/var/lib/rk3588-report-center"))
    bootstrap = os.environ.get(
        "RK3588_REPORT_CENTER_ADMIN_PASSWORD",
        str(section.get("bootstrap_admin_password", "")),
    )
    return ReportCenterConfig(
        enabled=bool(section.get("enabled", True)),
        shadow_mode=bool(section.get("shadow_mode", True)),
        intake_only=bool(section.get("intake_only", False)),
        data_dir=data_dir,
        database_path=str(section.get("database_path", f"{data_dir}/db/report-center.sqlite3")),
        archive_dir=str(section.get("archive_dir", f"{data_dir}/archive")),
        incoming_dir=str(section.get("incoming_dir", f"{data_dir}/incoming")),
        host=str(section.get("host", "0.0.0.0")),
        port=int(section.get("port", 8443)),
        ssl_cert=str(section.get("ssl_cert", f"{data_dir}/tls/report-center.crt")),
        ssl_key=str(section.get("ssl_key", f"{data_dir}/tls/report-center.key")),
        session_hours=max(1, int(section.get("session_hours", 12))),
        retention_days=max(1, int(section.get("retention_days", 90))),
        report_info_path=str(
            section.get("report_info_path", "/var/lib/rk3588-gateway/device/ReportInfo.xml")
        ),
        upload_poll_seconds=max(0.5, float(section.get("upload_poll_seconds", 2.0))),
        upload_retry_seconds=max(1, int(section.get("upload_retry_seconds", 60))),
        upload_max_attempts=max(1, int(section.get("upload_max_attempts", 10))),
        camera_loopback_only=bool(section.get("camera_loopback_only", True)),
        bootstrap_admin_password=bootstrap,
        portal_dir=str(section.get("portal_dir", "/opt/rk3588_gateway/portal_dist")),
        camera_configuration_image_dir=str(
            section.get(
                "camera_configuration_image_dir",
                "/run/rk3588-report-parser/configuration-images",
            )
        ),
        template_image_dir=str(
            section.get("template_image_dir", f"{data_dir}/template-images")
        ),
        camera_template_runtime_file=str(
            section.get(
                "camera_template_runtime_file",
                "/run/rk3588-report-parser/active-camera-template.json",
            )
        ),
        camera_full_page_once_file=str(
            section.get(
                "camera_full_page_once_file",
                "/run/rk3588-report-parser/force-full-page-once",
            )
        ),
        entry_capture_dir=str(
            section.get("entry_capture_dir", f"{data_dir}/entry-captures")
        ),
        hid_active_marker=str(
            section.get("hid_active_marker", "/run/rk3588-report-center/hid-active")
        ),
        external_report_token_file=str(
            section.get(
                "external_report_token_file",
                "/etc/gadget-msc-printer/report-link.token",
            )
        ),
        external_report_window_seconds=max(
            60,
            int(section.get("external_report_window_seconds", 7200)),
        ),
    )


def default_profile_snapshot(app_config: Any) -> dict[str, Any]:
    return {
        "name": str(app_config.active_profile or app_config.device.type or "default"),
        "patient_input_mode": "camera_direct",
        "camera_intake_enabled": True,
        "camera_patient_enabled": True,
        "patient_connector_id": None,
        "exam_item_filter": str(app_config.device.type or ""),
        "auto_entry_enabled": bool(app_config.hid_input.enabled),
        "report_source": "msc" if app_config.msc.enabled else "printer",
        "field_resolver": {"provider": "rules", "fields": []},
        "camera_template": {
            "schema_version": 1,
            "id": "default",
            "name": "申请单1",
            "mode": "fixed_roi",
            "selection_mode": "manual",
            "reference_capture_id": "",
            "canonical_image_size": [],
        },
        "hid": {
            "coordinate_mode": "legacy",
            "coordinate_basis": {
                "width": int(app_config.hid_input.screen_width),
                "height": int(app_config.hid_input.screen_height),
            },
            "template_path": app_config.hid_input.template_path,
            "actions": [],
        },
        "upload_target_id": None,
    }
