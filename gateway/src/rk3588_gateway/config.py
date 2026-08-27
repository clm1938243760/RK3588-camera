from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Union

import yaml


@dataclass(frozen=True)
class DeviceConfig:
    id: str
    location: str
    type: str
    profile_dir: str


@dataclass(frozen=True)
class SoftwareProfileConfig:
    id: str
    device_type: str
    software: str
    flow: str
    hid_input: dict[str, Any]
    vision: dict[str, Any]


@dataclass(frozen=True)
class ScannerConfig:
    enabled: bool
    event_device: str
    terminator_keys: list[str]
    min_length: int


@dataclass(frozen=True)
class PatientApiConfig:
    enabled: bool
    endpoint: str
    timeout_seconds: int
    user_agent: str
    raw_dir: str


@dataclass(frozen=True)
class HidInputConfig:
    enabled: bool
    keyboard_backend: str
    mouse_backend: str
    keyboard_device: str
    mouse_device: str
    ch9350_serial_device: str
    ch9350_baudrate: int
    ch9350_state: int
    ch9350_set_state2: bool
    ch9350_caps_led_mask: int
    ch9350_mouse_frame: str
    ch9350_mouse_reset_to_origin: bool
    template_path: str
    screen_width: int
    screen_height: int
    action_delay_ms: int
    start_delay_ms: int
    force_caps_ascii: bool
    non_ascii_mode: str
    powershell_wait_ms: int
    non_ascii_focus_clicks: int
    non_ascii_focus_click_interval_ms: int


@dataclass(frozen=True)
class VisionConfig:
    enabled: bool
    flow: str
    device: str
    capture_format: str
    capture_width: int
    capture_height: int
    capture_framerate: int
    capture_frames: int
    capture_io_mode: int
    workdir: str
    frame_endpoint: str
    icon_endpoint: str
    window_endpoint: str
    software: str
    wait_after_open: float
    wait_after_action: float
    wait_after_no_detection: float
    wait_after_start: float
    analysis_wait: float
    max_runtime: float
    timeout_seconds: float
    close_msc_popup_when_detected: bool


@dataclass(frozen=True)
class PrinterConfig:
    enabled: bool
    command: str
    printer_name: str
    timeout_seconds: int


@dataclass(frozen=True)
class PrintCaptureConfig:
    enabled: bool
    device: str
    output_dir: str
    chunk_size: int
    idle_complete_seconds: int
    min_job_bytes: int


@dataclass(frozen=True)
class ReportPdfConfig:
    enabled: bool
    output_dir: str
    keep_original: bool


@dataclass(frozen=True)
class ReportUploadConfig:
    enabled: bool
    endpoint: str
    report_info_path: str
    state_dir: str
    poll_interval_seconds: int
    timeout_seconds: int
    retry_interval_seconds: int
    max_attempts: int
    init_baseline: bool


@dataclass(frozen=True)
class MscConfig:
    enabled: bool
    image_path: str
    mount_dir: str
    output_dir: str
    state_dir: str
    gadget_dir: str
    udc_device: str
    poll_interval_seconds: int
    stable_seconds: int
    quiet_seconds: int
    init_baseline: bool
    rebuild_command: str
    copy_recursive: bool
    ignore_names: list[str]


@dataclass(frozen=True)
class GpioLineConfig:
    name: str
    enabled: bool
    backend: str
    chip: str
    line: int
    number: int
    direction: str
    active_low: bool
    default: int


@dataclass(frozen=True)
class GpioConfig:
    enabled: bool
    consumer: str
    lines: list[GpioLineConfig]


@dataclass(frozen=True)
class VmTransferConfig:
    enabled: bool
    method: str
    host: str
    user: str
    password: str
    remote_dir: str
    port: int
    connect_timeout_seconds: int
    keep_local_copy: bool


@dataclass(frozen=True)
class UploaderConfig:
    enabled: bool
    endpoint: str
    api_key: str
    timeout_seconds: int
    retry_interval_seconds: int
    max_batch_size: int


@dataclass(frozen=True)
class LocalApiConfig:
    enabled: bool
    host: str
    port: int
    display_state_path: str


@dataclass(frozen=True)
class StorageConfig:
    sqlite_path: str


@dataclass(frozen=True)
class LoggingConfig:
    level: str


@dataclass(frozen=True)
class AppConfig:
    active_profile: str
    profiles: dict[str, SoftwareProfileConfig]
    device: DeviceConfig
    scanner: ScannerConfig
    patient_api: PatientApiConfig
    hid_input: HidInputConfig
    vision: VisionConfig
    printer: PrinterConfig
    print_capture: PrintCaptureConfig
    report_pdf: ReportPdfConfig
    report_upload: ReportUploadConfig
    msc: MscConfig
    gpio: GpioConfig
    vm_transfer: VmTransferConfig
    uploader: UploaderConfig
    local_api: LocalApiConfig
    storage: StorageConfig
    logging: LoggingConfig


def _section(raw: dict[str, Any], name: str) -> dict[str, Any]:
    value = raw.get(name)
    if not isinstance(value, dict):
        raise ValueError(f"missing or invalid config section: {name}")
    return value


def _profile_from_mapping(profile_id: str, raw: dict[str, Any]) -> SoftwareProfileConfig:
    if not isinstance(raw, dict):
        raise ValueError(f"invalid profile: {profile_id}")
    resolved_id = str(raw.get("id", profile_id)).strip()
    if not resolved_id:
        raise ValueError("profile id must not be empty")
    vision = raw.get("vision", {})
    if not isinstance(vision, dict):
        raise ValueError(f"invalid profile vision section: {resolved_id}")
    hid_input = raw.get("hid_input", {})
    if not isinstance(hid_input, dict):
        raise ValueError(f"invalid profile hid_input section: {resolved_id}")
    return SoftwareProfileConfig(
        id=resolved_id,
        device_type=str(raw.get("device_type", "")),
        software=str(raw.get("software", "")),
        flow=str(raw.get("flow", "")),
        hid_input=dict(hid_input),
        vision=dict(vision),
    )


def _load_profiles_from_value(value: Any) -> dict[str, SoftwareProfileConfig]:
    if value in (None, ""):
        return {}
    if isinstance(value, dict):
        profiles = {}
        for profile_id, profile_raw in value.items():
            profile = _profile_from_mapping(str(profile_id), profile_raw)
            profiles[profile.id] = profile
        return profiles
    if isinstance(value, list):
        profiles = {}
        for index, profile_raw in enumerate(value):
            profile = _profile_from_mapping(str(index), profile_raw)
            profiles[profile.id] = profile
        return profiles
    raise ValueError("invalid config section: profiles")


def _load_profile_file(path: Path) -> dict[str, SoftwareProfileConfig]:
    with path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError(f"profile file root must be a mapping: {path}")
    if "profiles" in raw:
        return _load_profiles_from_value(raw.get("profiles"))
    profile = _profile_from_mapping(path.stem, raw)
    return {profile.id: profile}


def _load_profile_files(config_path: Path, raw: dict[str, Any]) -> dict[str, SoftwareProfileConfig]:
    profile_files = raw.get("profile_files", [])
    if not profile_files:
        return {}
    if isinstance(profile_files, (str, Path)):
        profile_files = [profile_files]
    if not isinstance(profile_files, list):
        raise ValueError("invalid config section: profile_files")
    profiles = {}
    for profile_file in profile_files:
        profile_path = Path(str(profile_file))
        if not profile_path.is_absolute():
            profile_path = config_path.parent / profile_path
        profiles.update(_load_profile_file(profile_path))
    return profiles


def load_config(path: Union[str, Path]) -> AppConfig:
    config_path = Path(path)
    with config_path.open("r", encoding="utf-8") as handle:
        raw = yaml.safe_load(handle) or {}
    if not isinstance(raw, dict):
        raise ValueError("config root must be a mapping")

    profiles = _load_profile_files(config_path, raw)
    profiles.update(_load_profiles_from_value(raw.get("profiles")))
    active_profile = str(raw.get("active_profile", "")).strip()
    selected_profile = profiles.get(active_profile) if active_profile else None
    if active_profile and selected_profile is None:
        raise ValueError(f"active_profile not found: {active_profile}")

    device = _section(raw, "device")
    scanner = _section(raw, "scanner")
    patient_api = _section(raw, "patient_api")
    hid_input = _section(raw, "hid_input")
    if selected_profile is not None:
        profile_hid_input = dict(hid_input)
        profile_hid_input.update(selected_profile.hid_input)
        hid_input = profile_hid_input
    vision = raw.get("vision", {})
    if not isinstance(vision, dict):
        raise ValueError("invalid config section: vision")
    if selected_profile is not None:
        profile_vision = dict(vision)
        profile_vision.update(selected_profile.vision)
        vision = profile_vision
    printer = _section(raw, "printer")
    print_capture = _section(raw, "print_capture")
    report_pdf = raw.get("report_pdf", {})
    if not isinstance(report_pdf, dict):
        raise ValueError("invalid config section: report_pdf")
    report_upload = raw.get("report_upload", {})
    if not isinstance(report_upload, dict):
        raise ValueError("invalid config section: report_upload")
    msc = raw.get("msc", {})
    if not isinstance(msc, dict):
        raise ValueError("invalid config section: msc")
    gpio = raw.get("gpio", {})
    if not isinstance(gpio, dict):
        raise ValueError("invalid config section: gpio")
    vm_transfer = _section(raw, "vm_transfer")
    uploader = _section(raw, "uploader")
    local_api = _section(raw, "local_api")
    storage = _section(raw, "storage")
    logging = _section(raw, "logging")
    report_info_path = str(report_upload.get("report_info_path", "/var/lib/rk3588-gateway/device/ReportInfo.xml"))
    if report_info_path == "/var/lib/rk3588-gateway/ReportInfo.xml":
        report_info_path = "/var/lib/rk3588-gateway/device/ReportInfo.xml"

    return AppConfig(
        active_profile=active_profile,
        profiles=profiles,
        device=DeviceConfig(
            id=str(device.get("id", "rk3588-gateway")),
            location=str(device.get("location", "")),
            type=selected_profile.device_type if selected_profile and selected_profile.device_type else str(device.get("type", "人体成分检查")),
            profile_dir=str(device.get("profile_dir", "/var/lib/rk3588-gateway/device")),
        ),
        scanner=ScannerConfig(
            enabled=bool(scanner.get("enabled", True)),
            event_device=str(scanner.get("event_device", "")),
            terminator_keys=list(scanner.get("terminator_keys", ["KEY_ENTER"])),
            min_length=int(scanner.get("min_length", 1)),
        ),
        patient_api=PatientApiConfig(
            enabled=bool(patient_api.get("enabled", True)),
            endpoint=str(patient_api.get("endpoint", "")),
            timeout_seconds=int(patient_api.get("timeout_seconds", 10)),
            user_agent=str(patient_api.get("user_agent", "RK3588-Gateway")),
            raw_dir=str(patient_api.get("raw_dir", "/var/lib/rk3588-gateway/api_raw")),
        ),
        hid_input=HidInputConfig(
            enabled=bool(hid_input.get("enabled", True)),
            keyboard_backend=str(hid_input.get("keyboard_backend", "usb_gadget")),
            mouse_backend=str(hid_input.get("mouse_backend", "usb_gadget")),
            keyboard_device=str(hid_input.get("keyboard_device", "/dev/hidg0")),
            mouse_device=str(hid_input.get("mouse_device", "/dev/hidg1")),
            ch9350_serial_device=str(hid_input.get("ch9350_serial_device", "")),
            ch9350_baudrate=int(hid_input.get("ch9350_baudrate", 115200)),
            ch9350_state=int(hid_input.get("ch9350_state", 0)),
            ch9350_set_state2=bool(hid_input.get("ch9350_set_state2", False)),
            ch9350_caps_led_mask=int(hid_input.get("ch9350_caps_led_mask", 1)),
            ch9350_mouse_frame=str(hid_input.get("ch9350_mouse_frame", "absolute7")),
            ch9350_mouse_reset_to_origin=bool(hid_input.get("ch9350_mouse_reset_to_origin", False)),
            template_path=str(hid_input.get("template_path", "/opt/rk3588_gateway/MarkInfo_SearchTitle_Config_100.json")),
            screen_width=int(hid_input.get("screen_width", 1920)),
            screen_height=int(hid_input.get("screen_height", 1080)),
            action_delay_ms=int(hid_input.get("action_delay_ms", 120)),
            start_delay_ms=int(hid_input.get("start_delay_ms", 300)),
            force_caps_ascii=bool(hid_input.get("force_caps_ascii", True)),
            non_ascii_mode=str(hid_input.get("non_ascii_mode", "alt_numpad_hex")),
            powershell_wait_ms=int(hid_input.get("powershell_wait_ms", 2500)),
            non_ascii_focus_clicks=int(hid_input.get("non_ascii_focus_clicks", 1)),
            non_ascii_focus_click_interval_ms=int(hid_input.get("non_ascii_focus_click_interval_ms", 100)),
        ),
        vision=VisionConfig(
            enabled=bool(vision.get("enabled", False)),
            flow=selected_profile.flow if selected_profile and selected_profile.flow else str(vision.get("flow", "body_composition")),
            device=str(vision.get("device", "/dev/video40")),
            capture_format=str(vision.get("capture_format", "bgr")),
            capture_width=int(vision.get("capture_width", 1920)),
            capture_height=int(vision.get("capture_height", 1080)),
            capture_framerate=int(vision.get("capture_framerate", 60)),
            capture_frames=int(vision.get("capture_frames", 1)),
            capture_io_mode=int(vision.get("capture_io_mode", 0)),
            workdir=str(vision.get("workdir", "/tmp/rk3588-vision-flow")),
            frame_endpoint=str(vision.get("frame_endpoint", "")),
            icon_endpoint=str(vision.get("icon_endpoint", "http://127.0.0.1:5002/icon/locate")),
            window_endpoint=str(vision.get("window_endpoint", "http://127.0.0.1:5002/window/detect")),
            software=selected_profile.software if selected_profile and selected_profile.software else str(vision.get("software", "人体成分分析仪")),
            wait_after_open=float(vision.get("wait_after_open", 2.5)),
            wait_after_action=float(vision.get("wait_after_action", 1.0)),
            wait_after_no_detection=float(vision.get("wait_after_no_detection", 5.0)),
            wait_after_start=float(vision.get("wait_after_start", 3.0)),
            analysis_wait=float(vision.get("analysis_wait", 1.5)),
            max_runtime=float(vision.get("max_runtime", 300.0)),
            timeout_seconds=float(vision.get("timeout_seconds", 15.0)),
            close_msc_popup_when_detected=bool(vision.get("close_msc_popup_when_detected", True)),
        ),
        printer=PrinterConfig(
            enabled=bool(printer.get("enabled", True)),
            command=str(printer.get("command", "lp")),
            printer_name=str(printer.get("printer_name", "")),
            timeout_seconds=int(printer.get("timeout_seconds", 15)),
        ),
        print_capture=PrintCaptureConfig(
            enabled=bool(print_capture.get("enabled", True)),
            device=str(print_capture.get("device", "/dev/g_printer0")),
            output_dir=str(print_capture.get("output_dir", "/var/lib/rk3588-gateway/print_jobs")),
            chunk_size=int(print_capture.get("chunk_size", 65536)),
            idle_complete_seconds=int(print_capture.get("idle_complete_seconds", 2)),
            min_job_bytes=int(print_capture.get("min_job_bytes", 128)),
        ),
        report_pdf=ReportPdfConfig(
            enabled=bool(report_pdf.get("enabled", True)),
            output_dir=str(report_pdf.get("output_dir", "/var/lib/rk3588-gateway/reports_pdf")),
            keep_original=bool(report_pdf.get("keep_original", True)),
        ),
        report_upload=ReportUploadConfig(
            enabled=bool(report_upload.get("enabled", False)),
            endpoint=str(report_upload.get("endpoint", "")),
            report_info_path=report_info_path,
            state_dir=str(report_upload.get("state_dir", "/var/lib/rk3588-gateway/report_upload_state")),
            poll_interval_seconds=int(report_upload.get("poll_interval_seconds", 5)),
            timeout_seconds=int(report_upload.get("timeout_seconds", 30)),
            retry_interval_seconds=int(report_upload.get("retry_interval_seconds", 60)),
            max_attempts=int(report_upload.get("max_attempts", 3)),
            init_baseline=bool(report_upload.get("init_baseline", True)),
        ),
        msc=MscConfig(
            enabled=bool(msc.get("enabled", False)),
            image_path=str(msc.get("image_path", "/var/lib/rk3588-gateway/msc/ums_shared.img")),
            mount_dir=str(msc.get("mount_dir", "/mnt/rk3588-gateway-msc")),
            output_dir=str(msc.get("output_dir", "/var/lib/rk3588-gateway/msc_files")),
            state_dir=str(msc.get("state_dir", "/var/lib/rk3588-gateway/msc_state")),
            gadget_dir=str(msc.get("gadget_dir", "/sys/kernel/config/usb_gadget/rk3588_c1_msc")),
            udc_device=str(msc.get("udc_device", "fc400000.usb")),
            poll_interval_seconds=int(msc.get("poll_interval_seconds", 5)),
            stable_seconds=int(msc.get("stable_seconds", 3)),
            quiet_seconds=int(msc.get("quiet_seconds", 2)),
            init_baseline=bool(msc.get("init_baseline", True)),
            rebuild_command=str(msc.get("rebuild_command", "")),
            copy_recursive=bool(msc.get("copy_recursive", True)),
            ignore_names=list(msc.get("ignore_names", ["System Volume Information", "$RECYCLE.BIN"])),
        ),
        gpio=GpioConfig(
            enabled=bool(gpio.get("enabled", False)),
            consumer=str(gpio.get("consumer", "rk3588-gateway")),
            lines=[
                GpioLineConfig(
                    name=str(item.get("name", f"gpio{index + 1}")),
                    enabled=bool(item.get("enabled", False)),
                    backend=str(item.get("backend", "gpiod")).lower(),
                    chip=str(item.get("chip", "/dev/gpiochip0")),
                    line=int(item.get("line", 0)),
                    number=int(item.get("number", item.get("line", 0))),
                    direction=str(item.get("direction", "out")).lower(),
                    active_low=bool(item.get("active_low", False)),
                    default=int(item.get("default", 0)),
                )
                for index, item in enumerate(gpio.get("lines", []))
                if isinstance(item, dict)
            ],
        ),
        vm_transfer=VmTransferConfig(
            enabled=bool(vm_transfer.get("enabled", False)),
            method=str(vm_transfer.get("method", "scp")),
            host=str(vm_transfer.get("host", "")),
            user=str(vm_transfer.get("user", "")),
            password=str(vm_transfer.get("password", "")),
            remote_dir=str(vm_transfer.get("remote_dir", "~/Documents")),
            port=int(vm_transfer.get("port", 22)),
            connect_timeout_seconds=int(vm_transfer.get("connect_timeout_seconds", 10)),
            keep_local_copy=bool(vm_transfer.get("keep_local_copy", True)),
        ),
        uploader=UploaderConfig(
            enabled=bool(uploader.get("enabled", True)),
            endpoint=str(uploader.get("endpoint", "")),
            api_key=str(uploader.get("api_key", "")),
            timeout_seconds=int(uploader.get("timeout_seconds", 10)),
            retry_interval_seconds=int(uploader.get("retry_interval_seconds", 5)),
            max_batch_size=int(uploader.get("max_batch_size", 20)),
        ),
        local_api=LocalApiConfig(
            enabled=bool(local_api.get("enabled", True)),
            host=str(local_api.get("host", "0.0.0.0")),
            port=int(local_api.get("port", 8080)),
            display_state_path=str(
                local_api.get("display_state_path", "/run/rk3588-gateway/display-state.json")
            ),
        ),
        storage=StorageConfig(sqlite_path=str(storage.get("sqlite_path", "events.db"))),
        logging=LoggingConfig(level=str(logging.get("level", "INFO")).upper()),
    )
