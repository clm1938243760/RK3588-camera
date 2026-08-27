from __future__ import annotations

import importlib.util
import json
import tempfile
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageChops


def _load_fb_status():
    script = Path(__file__).resolve().parents[1] / "scripts" / "fb_status.py"
    spec = importlib.util.spec_from_file_location("fb_status", script)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_ili9488_write_supports_pillow_without_resampling(monkeypatch) -> None:
    module = _load_fb_status()
    source = Image.new("RGB", (1, 1), (20, 40, 60))
    monkeypatch.setattr(module, "Image", SimpleNamespace(BILINEAR=Image.BILINEAR))

    display = module.Ili9488Display.__new__(module.Ili9488Display)
    display.width = 2
    display.height = 2
    display.pixel_format = 18
    display.color_order = "rgb"
    display.cs = None
    display.dc = SimpleNamespace(set=lambda value: None)
    payloads: list[bytes] = []
    display.spi = SimpleNamespace(write=payloads.append)
    display._set_window = lambda *args: None

    display.write(source)

    assert payloads == [bytes((20, 40, 60)) * 4]


def test_ili9488_only_writes_changed_rectangle_after_first_frame() -> None:
    module = _load_fb_status()
    display = module.Ili9488Display.__new__(module.Ili9488Display)
    display.width = 4
    display.height = 3
    display.pixel_format = 18
    display.color_order = "rgb"
    display.cs = None
    display.dc = SimpleNamespace(set=lambda value: None)
    windows: list[tuple[int, int, int, int]] = []
    payloads: list[bytes] = []
    display.spi = SimpleNamespace(write=payloads.append)
    display._set_window = lambda *args: windows.append(args)

    first = Image.new("RGB", (4, 3), (0, 0, 0))
    second = first.copy()
    second.putpixel((2, 1), (255, 0, 0))
    display.write(first)
    display.write(second)
    display.write(second)

    assert windows == [(0, 0, 3, 2), (2, 1, 2, 1)]
    assert [len(payload) for payload in payloads] == [36, 3]
    assert payloads[1] == bytes((255, 0, 0))


def test_systemd_service_uses_verified_rk3588_spi_device() -> None:
    service = (
        Path(__file__).resolve().parents[1]
        / "systemd"
        / "rk3588-fb-status.service"
    ).read_text(encoding="utf-8")

    assert "--spidev /dev/spidev3.0" in service
    assert "--spi-speed 4000000" in service
    assert "--dc-gpio 133" in service
    assert "--reset-gpio -1" in service
    assert "--cs-gpio 134" in service
    assert "--interval 0.1" in service
    assert "--camera-status-file /run/rk3588-report-parser/camera-trigger.json" in service
    assert "--camera-status-url http://127.0.0.1:8893/api/status" in service
    assert "/dev/spidev0.0" not in service


def test_camera_status_file_matches_web_paper_outline_rule() -> None:
    module = _load_fb_status()
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "camera-trigger.json"
        payload = {
            "paper_detected": True,
            "paper_corners": [[1, 1], [10, 1], [10, 10], [1, 10]],
            "state": "tracking",
            "capture_id": "a" * 32,
        }
        path.write_text(json.dumps(payload), encoding="utf-8")
        status = module.read_camera_status_file(str(path))
        payload["paper_corners"] = payload["paper_corners"][:3]
        path.write_text(json.dumps(payload), encoding="utf-8")
        without_outline = module.read_camera_status_file(str(path))

    assert status["paper_detected"] is True
    assert status["capture_stage"] == "tracking"
    assert without_outline["paper_detected"] is False


def test_camera_status_source_prefers_local_file(monkeypatch) -> None:
    module = _load_fb_status()
    expected = {"paper_detected": True, "capture_stage": "tracking"}
    monkeypatch.setattr(module, "read_camera_status_file", lambda path: expected)
    monkeypatch.setattr(
        module,
        "read_camera_status",
        lambda url: (_ for _ in ()).throw(AssertionError("HTTP fallback should not run")),
    )

    assert module.read_camera_status_source("/run/camera.json", "http://127.0.0.1") is expected


def test_ili9488_initialization_uses_verified_panel_sequence(monkeypatch) -> None:
    module = _load_fb_status()
    monkeypatch.setattr(module.time, "sleep", lambda seconds: None)
    commands: list[tuple[int, bytes]] = []

    display = module.Ili9488Display.__new__(module.Ili9488Display)
    display.reset = None
    display.cs = None
    display.bl = None
    display.rotate = 270
    display.color_order = "rgb"
    display.pixel_format = 18
    display.invert = False
    display.command = lambda command, data=b"": commands.append((command, data))

    display._init_panel()

    assert commands[:2] == [(0x01, b""), (0x11, b"")]
    assert (0xE0, bytes((0x00, 0x03, 0x09, 0x08, 0x16, 0x0A, 0x3F, 0x78, 0x4C, 0x09, 0x0A, 0x08, 0x16, 0x1A, 0x0F))) in commands
    assert (0xE1, bytes((0x00, 0x16, 0x19, 0x03, 0x0F, 0x05, 0x32, 0x45, 0x46, 0x04, 0x0E, 0x0D, 0x35, 0x37, 0x0F))) in commands
    assert commands[-4:] == [
        (0xE9, b"\x00"),
        (0xF7, bytes((0xA9, 0x51, 0x2C, 0x82))),
        (0x20, b""),
        (0x29, b""),
    ]


def test_ili9488_command_controls_software_chip_select() -> None:
    module = _load_fb_status()
    events: list[tuple[str, object]] = []
    display = module.Ili9488Display.__new__(module.Ili9488Display)
    display.cs = SimpleNamespace(set=lambda value: events.append(("cs", value)))
    display.dc = SimpleNamespace(set=lambda value: events.append(("dc", value)))
    display.spi = SimpleNamespace(write=lambda data: events.append(("spi", data)))

    display.command(0x3A, b"\x66")

    assert events == [
        ("cs", False),
        ("dc", False),
        ("spi", b"\x3a"),
        ("dc", True),
        ("spi", b"\x66"),
        ("cs", True),
    ]


def test_upload_screens_use_medical_status_colors() -> None:
    module = _load_fb_status()
    renderer = module.AssetRenderer(Path("missing-assets"))

    uploading = renderer.render({"display": {"screen": "report_uploading"}})
    success = renderer.render({"display": {"screen": "report_upload_success"}})
    failed = renderer.render({"display": {"screen": "report_upload_failed"}})

    assert uploading.getpixel((240, 97)) == (42, 125, 164)
    assert success.getpixel((240, 97)) == (35, 145, 101)
    assert failed.getpixel((240, 97)) == (190, 72, 72)


def test_entry_status_screens_use_medical_status_colors() -> None:
    module = _load_fb_status()
    renderer = module.AssetRenderer(Path("missing-assets"))

    detected = renderer.render({"display": {"screen": "report_detecting"}})
    completed = renderer.render({"display": {"screen": "entry_completed"}})

    assert detected.getpixel((240, 97)) == (42, 125, 164)
    assert completed.getpixel((240, 97)) == (35, 145, 101)


def test_camera_status_maps_detection_but_not_intermediate_ocr_confidence() -> None:
    module = _load_fb_status()
    merger = module.CameraStatusMerger()

    detected = merger.merge(
        {"display": {"screen": "wait_scan"}},
        {"paper_detected": True, "capture_stage": "tracking"},
    )
    intermediate_review = merger.merge(
        {"display": {"screen": "report_detecting"}},
        {
            "paper_detected": True,
            "capture_stage": "completed",
            "full_text": {"status": "review_required"},
        },
    )
    protected = merger.merge(
        {"display": {"screen": "inputting", "patient_name": "A"}},
        {"paper_detected": True, "capture_stage": "completed"},
    )
    after_expired_result = merger.merge(
        {
            "display": {"screen": "entry_completed"},
            "expires_at": 0,
        },
        {"paper_detected": True, "capture_stage": "tracking"},
    )
    rejected_state = {
        "display": {"screen": "paper_reposition", "capture_id": "capture-1"},
        "updated_at": 1,
    }
    final_rejection = merger.merge(
        rejected_state,
        {"paper_detected": True, "capture_stage": "completed", "capture_id": "capture-1"},
    )
    cleared = merger.merge(
        rejected_state,
        {"paper_detected": False, "capture_stage": "absent"},
    )
    next_paper = merger.merge(
        rejected_state,
        {"paper_detected": True, "capture_stage": "tracking", "capture_id": ""},
    )
    next_rejection = merger.merge(
        {
            "display": {"screen": "paper_reposition", "capture_id": "capture-2"},
            "updated_at": 2,
        },
        {"paper_detected": True, "capture_stage": "completed", "capture_id": "capture-2"},
    )

    assert detected["display"]["screen"] == "report_detecting"
    assert detected["display"]["progress_text"] == "检测纸张稳定"
    assert intermediate_review["display"]["screen"] == "report_detecting"
    assert intermediate_review["display"]["progress_text"] == "生成结构化字段"
    assert protected["display"]["screen"] == "inputting"
    assert after_expired_result["display"]["screen"] == "report_detecting"
    assert final_rejection["display"]["screen"] == "paper_reposition"
    assert cleared["display"]["screen"] == "wait_scan"
    assert next_paper["display"]["screen"] == "report_detecting"
    assert next_rejection["display"]["screen"] == "paper_reposition"


def test_entry_completed_stays_visible_until_matching_paper_is_removed() -> None:
    module = _load_fb_status()
    merger = module.CameraStatusMerger()
    completed_state = {
        "display": {
            "screen": "entry_completed",
            "patient_name": "A",
            "patient_id": "60019825336",
            "capture_id": "capture-1",
        },
        "expires_at": 0,
    }

    latched = merger.merge(
        completed_state,
        {
            "paper_detected": True,
            "capture_stage": "completed",
            "reason": "waiting_for_paper_removal",
            "capture_id": "capture-1",
        },
    )
    removed = merger.merge(
        completed_state,
        {"paper_detected": False, "capture_stage": "absent", "capture_id": "capture-1"},
    )
    next_paper = merger.merge(
        completed_state,
        {"paper_detected": True, "capture_stage": "tracking", "capture_id": "capture-2"},
    )

    assert latched["display"]["screen"] == "entry_completed"
    assert latched["display"]["prompt_text"] == "请移除申请单"
    assert "expires_at" not in latched
    assert removed["display"]["screen"] == "wait_scan"
    assert next_paper["display"]["screen"] == "report_detecting"


def test_upload_result_returns_to_completed_until_paper_is_removed(monkeypatch) -> None:
    module = _load_fb_status()
    merger = module.CameraStatusMerger()
    monkeypatch.setattr(module.time, "time", lambda: 100.0)
    camera = {
        "paper_detected": True,
        "capture_stage": "completed",
        "reason": "waiting_for_paper_removal",
        "capture_id": "capture-1",
    }

    merger.merge(
        {
            "display": {
                "screen": "entry_completed",
                "patient_name": "A",
                "patient_id": "60019825336",
                "capture_id": "capture-1",
            },
            "expires_at": 103.0,
        },
        camera,
    )
    uploading = merger.merge(
        {"display": {"screen": "report_uploading"}},
        camera,
    )
    upload_success = merger.merge(
        {
            "display": {"screen": "report_upload_success"},
            "expires_at": 103.0,
        },
        camera,
    )
    after_success = merger.merge(
        {
            "display": {"screen": "report_upload_success"},
            "expires_at": 99.0,
        },
        camera,
    )
    removed = merger.merge(
        {
            "display": {"screen": "report_upload_success"},
            "expires_at": 99.0,
        },
        {**camera, "paper_detected": False},
    )

    assert uploading["display"]["screen"] == "report_uploading"
    assert upload_success["display"]["screen"] == "report_upload_success"
    assert after_success["display"]["screen"] == "entry_completed"
    assert after_success["display"]["prompt_text"] == "请移除申请单"
    assert after_success["display"]["patient_name"] == "A"
    assert removed["display"]["screen"] == "wait_scan"


def test_expired_upload_result_recovers_completed_state_after_display_restart(monkeypatch) -> None:
    module = _load_fb_status()
    merger = module.CameraStatusMerger()
    monkeypatch.setattr(module.time, "time", lambda: 100.0)

    recovered = merger.merge(
        {
            "display": {"screen": "report_upload_success"},
            "expires_at": 99.0,
        },
        {
            "paper_detected": True,
            "capture_stage": "completed",
            "reason": "waiting_for_paper_removal",
            "capture_id": "capture-1",
        },
    )

    assert recovered["display"]["screen"] == "entry_completed"
    assert recovered["display"]["prompt_text"] == "请移除申请单"


def test_entry_completed_renders_remove_paper_prompt(monkeypatch) -> None:
    module = _load_fb_status()
    renderer = module.AssetRenderer(Path("missing-assets"))
    rendered_text: list[str] = []
    original_center_text = renderer._center_text

    def record_center_text(draw, text, y, font, fill):
        rendered_text.append(text)
        return original_center_text(draw, text, y, font, fill)

    monkeypatch.setattr(renderer, "_center_text", record_center_text)
    renderer.render({"display": {"screen": "entry_completed"}})

    assert "请移除申请单" in rendered_text


def test_render_signature_ignores_display_heartbeat() -> None:
    module = _load_fb_status()
    first = module.render_signature({"display": {"screen": "wait_scan"}, "updated_at": 1})
    second = module.render_signature({"display": {"screen": "wait_scan"}, "updated_at": 2})

    assert first == second


def test_entry_status_screens_keep_board_ip_in_lower_right(monkeypatch) -> None:
    module = _load_fb_status()
    renderer = module.AssetRenderer(Path("missing-assets"))
    monkeypatch.setattr(module, "board_ip", lambda: "192.0.2.10")
    with_ip = renderer.render({"display": {"screen": "entry_completed"}})
    monkeypatch.setattr(module, "board_ip", lambda: "")
    without_ip = renderer.render({"display": {"screen": "entry_completed"}})

    difference = ImageChops.difference(with_ip, without_ip)
    bbox = difference.getbbox()
    assert bbox is not None
    assert bbox[0] > 180
    assert bbox[2] > 440
    assert bbox[1] >= 270


def test_expired_upload_result_returns_to_waiting_screen(monkeypatch) -> None:
    module = _load_fb_status()
    renderer = module.AssetRenderer(Path("missing-assets"))
    monkeypatch.setattr(module.time, "time", lambda: 100.0)

    expired = renderer.render({
        "display": {"screen": "report_upload_failed"},
        "expires_at": 99.0,
    })

    assert expired.getpixel((240, 97)) == (21, 137, 132)


def test_upload_result_transition_does_not_block_worker(monkeypatch) -> None:
    module = _load_fb_status()
    renderer = module.AssetRenderer(Path("missing-assets"))
    monkeypatch.setattr(module.time, "time", lambda: 100.0)
    state = {
        "display": {"screen": "report_upload_success"},
        "leading_display": {"screen": "report_uploading"},
        "display_not_before": 101.0,
        "expires_at": 109.0,
    }

    leading = renderer.render(state)
    uploading_signature = module.render_signature({"display": {"screen": "report_uploading"}})
    leading_signature = module.render_signature(state)
    monkeypatch.setattr(module.time, "time", lambda: 102.0)
    result = renderer.render(state)
    result_signature = module.render_signature(state)

    assert leading.getpixel((240, 97)) == (42, 125, 164)
    assert result.getpixel((240, 97)) == (35, 145, 101)
    assert leading_signature == uploading_signature
    assert result_signature != leading_signature
