import asyncio
import importlib.util
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from PIL import Image, ImageDraw


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


def load_module():
    script = SRC / "rk3588_gateway" / "vision_flow.py"
    spec = importlib.util.spec_from_file_location("rk3588_gateway.vision_flow", script)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    spec.loader.exec_module(module)
    return module


class FakeHidOutput:
    def __init__(self):
        self.clicks = []
        self.forms = []
        self.inputs = []

    async def click(self, x, y):
        self.clicks.append((x, y))

    async def double_click(self, x, y):
        self.clicks.append((x, y, "double"))

    async def input_text(self, text, x, y, field=""):
        self.inputs.append((text, x, y, field))

    async def execute_form(self, task):
        self.forms.append(task)


class FakeVisionFlow:
    def __init__(self, responses, open_results=None, flow="body_composition"):
        vision = load_module()
        self._impl = vision.VisionFlow(
            SimpleNamespace(
                enabled=True,
                flow=flow,
                device="/dev/video40",
                capture_format="bgr",
                capture_width=1920,
                capture_height=1080,
                capture_framerate=60,
                capture_frames=1,
                capture_io_mode=0,
                workdir="/tmp/test-vision",
                icon_endpoint="http://127.0.0.1/icon",
                window_endpoint="http://127.0.0.1/window",
                software="人体成分分析仪",
                wait_after_open=0.0,
                wait_after_action=0.0,
                wait_after_no_detection=5.0,
                wait_after_start=0.0,
                analysis_wait=0.0,
                max_runtime=5.0,
                timeout_seconds=1.0,
                close_msc_popup_after_report=False,
                close_msc_popup_when_detected=True,
            ),
            FakeHidOutput(),
        )
        self.responses = list(responses)
        self.open_results = list(open_results or [])
        self.open_count = 0
        self.clicked_texts = []
        self.sleeps = []
        self._impl.detect_window = self.detect_window
        self._impl.open_app = self.open_app
        self._impl.click_ocr_text = self.click_ocr_text
        self._impl.detect_ocr_roi = self.detect_ocr_roi
        self._impl.sleep = self.sleep

    @property
    def hid_output(self):
        return self._impl.hid_output

    async def run_until_form_done(self, task):
        return await self._impl.run_until_form_done(task)

    async def detect_window(self, image_name):
        if not self.responses:
            raise AssertionError("unexpected detect_window call")
        return self.responses.pop(0)

    async def detect_ocr_roi(self, image_name, roi_box):
        return await self.detect_window(image_name)

    async def open_app(self, image_name):
        self.open_count += 1
        if self.open_results:
            return self.open_results.pop(0)
        return True

    async def click_ocr_text(self, response, text):
        self.clicked_texts.append(text)
        return True

    async def sleep(self, seconds):
        self.sleeps.append(seconds)
        return None


class VisionFlowTest(unittest.TestCase):
    def test_bodypass_main_window_accepts_mojibake_title_with_bodypas(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "box": [0, 0, 1920, 1080],
                    "ocr": [
                        {
                            "text": "浣撴垚鍒嗘暟鎹鐞嗙▼搴忥紙BodyPas",
                            "center": [590, 188],
                            "box": [504, 180, 676, 195],
                        },
                        {
                            "text": "Body Pass绋?",
                            "center": [666, 214],
                            "box": [620, 209, 712, 220],
                        },
                    ],
                }
            ]
        }

        self.assertTrue(vision.is_bodypass_main_window(response))
        self.assertEqual(vision.bodypass_window_box(response), (467, 166, 1479, 895))

    def test_bodypass_fixed_input_centers_use_fallback_box_for_fullscreen_window(self):
        vision = load_module()
        window = {"box": [0, 0, 1920, 1080], "ocr": []}

        self.assertEqual(vision.bodypass_fixed_input_center(window, vision.BODYPASS_MEMBER_ID_OFFSET), (685, 362))
        self.assertEqual(vision.bodypass_fixed_input_center(window, vision.BODYPASS_MEMBER_NAME_OFFSET), (685, 390))

    def test_bodypass_print_dialog_prefers_ocr_button_center_over_window_offset(self):
        flow = FakeVisionFlow([], flow="bodypass")
        response = {
            "windows": [
                {
                    "box": [0, 0, 1920, 1080],
                    "ocr": [
                        {"text": "选择打印机", "center": [620, 450]},
                        {"text": "打印(P)", "center": [875, 870]},
                    ],
                }
            ]
        }

        asyncio.run(flow._impl.click_bodypass_print_dialog(response))

        self.assertEqual(flow.hid_output.clicks, [(875, 870)])

    def test_bodypass_print_dialog_uses_fullscreen_fixed_center_when_button_ocr_is_missing(self):
        flow = FakeVisionFlow([], flow="bodypass")
        response = {
            "windows": [
                {
                    "box": [0, 0, 1920, 1080],
                    "ocr": [
                        {"text": "选择打印机", "center": [620, 450]},
                        {"text": "打印到文件(F)", "center": [822, 596]},
                    ],
                }
            ]
        }

        asyncio.run(flow._impl.click_bodypass_print_dialog(response))

        self.assertEqual(flow.hid_output.clicks, [(867, 870)])

    def test_capture_jpeg_from_endpoint_writes_valid_jpeg(self):
        vision = load_module()
        original_urlopen = vision.urllib.request.urlopen
        image_bytes = b"\xff\xd8" + (b"x" * (vision.MIN_CAPTURE_FRAME_BYTES + 8)) + b"\xff\xd9"

        class FakeResponse:
            status = 200

            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc, tb):
                return False

            def read(self):
                return image_bytes

        def fake_urlopen(request, timeout):
            self.assertEqual(timeout, 1.0)
            self.assertEqual(request.full_url, "http://127.0.0.1:8090/api/frame.jpg")
            return FakeResponse()

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "frame.jpg"
            try:
                vision.urllib.request.urlopen = fake_urlopen
                vision.capture_jpeg_from_endpoint("http://127.0.0.1:8090/api/frame.jpg", output, timeout=1.0)
            finally:
                vision.urllib.request.urlopen = original_urlopen

            self.assertEqual(output.read_bytes(), image_bytes)

    def test_capture_screen_uses_frame_endpoint_before_local_capture(self):
        vision = load_module()
        original_endpoint_capture = vision.capture_jpeg_from_endpoint
        original_capture_jpeg = vision.capture_jpeg
        endpoint_calls = []
        local_calls = []

        def fake_endpoint_capture(endpoint, output, timeout, **kwargs):
            endpoint_calls.append((endpoint, output, timeout, kwargs))
            output.write_bytes(b"\xff\xd8" + (b"x" * (vision.MIN_CAPTURE_FRAME_BYTES + 8)) + b"\xff\xd9")

        def fake_capture_jpeg(*args, **kwargs):
            local_calls.append((args, kwargs))

        try:
            vision.capture_jpeg_from_endpoint = fake_endpoint_capture
            vision.capture_jpeg = fake_capture_jpeg
            flow = vision.VisionFlow(
                SimpleNamespace(
                    enabled=True,
                    device="/dev/video40",
                    capture_format="bgr",
                    capture_width=1920,
                    capture_height=1080,
                    capture_framerate=60,
                    capture_frames=1,
                    capture_io_mode=0,
                    workdir="/tmp/test-vision",
                    frame_endpoint="http://127.0.0.1:8090/api/frame.jpg",
                    icon_endpoint="http://127.0.0.1/icon",
                    window_endpoint="http://127.0.0.1/window",
                    software="test",
                    wait_after_open=0.0,
                    wait_after_action=0.0,
                    wait_after_no_detection=5.0,
                    wait_after_start=0.0,
                    analysis_wait=0.0,
                    max_runtime=5.0,
                    timeout_seconds=1.0,
                    close_msc_popup_when_detected=False,
                ),
                FakeHidOutput(),
            )

            with tempfile.TemporaryDirectory() as temp_dir:
                asyncio.run(flow.capture_screen(Path(temp_dir) / "frame.jpg"))
        finally:
            vision.capture_jpeg_from_endpoint = original_endpoint_capture
            vision.capture_jpeg = original_capture_jpeg

        self.assertEqual(len(endpoint_calls), 1)
        self.assertEqual(local_calls, [])

    def test_build_capture_command_uses_rk3588_hdmi_rx_bgr_frame(self):
        vision = load_module()

        cmd = vision.build_capture_command(
            "/dev/video40",
            Path("/tmp/vision/window_1.jpg"),
            width=1920,
            height=1080,
            framerate=60,
            frames=1,
            io_mode=0,
            capture_format="bgr",
        )

        self.assertIn("device=/dev/video40", cmd)
        self.assertNotIn("io-mode=0", cmd)
        self.assertIn("num-buffers=1", cmd)
        self.assertIn("video/x-raw,format=BGR,width=1920,height=1080,framerate=60/1", cmd)
        self.assertIn("videoconvert", cmd)
        self.assertIn("jpegenc", cmd)
        self.assertIn("quality=90", cmd)
        self.assertIn("multifilesink", cmd)
        self.assertIn("location=/tmp/vision/.window_1_%02d.jpg", cmd)

    def test_build_capture_command_still_supports_uvc_mjpg_stable_frame(self):
        vision = load_module()

        cmd = vision.build_capture_command(
            "/dev/video9",
            Path("/tmp/vision/window_1.jpg"),
            width=1920,
            height=1080,
            framerate=30,
            frames=30,
            io_mode=2,
            capture_format="mjpg",
        )

        self.assertIn("device=/dev/video9", cmd)
        self.assertIn("io-mode=2", cmd)
        self.assertIn("num-buffers=30", cmd)
        self.assertIn("image/jpeg,width=1920,height=1080,framerate=30/1", cmd)
        self.assertIn("multifilesink", cmd)
        self.assertIn("location=/tmp/vision/.window_1_%02d.jpg", cmd)

    def test_select_capture_frame_prefers_largest_frame_over_last_frame(self):
        vision = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "shot.jpg"
            (Path(temp_dir) / ".shot_00.jpg").write_bytes(b"\xff\xd8" + b"0" * 41654 + b"\xff\xd9")
            (Path(temp_dir) / ".shot_28.jpg").write_bytes(b"\xff\xd8" + b"1" * 187784 + b"\xff\xd9")
            (Path(temp_dir) / ".shot_29.jpg").write_bytes(b"\xff\xd8" + b"2" * 41105 + b"\xff\xd9")

            selected = vision.select_capture_frame(output, frames=30)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.name, ".shot_28.jpg")

    def test_select_capture_frame_ignores_large_non_jpeg_frame(self):
        vision = load_module()

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "shot.jpg"
            (Path(temp_dir) / ".shot_28.jpg").write_bytes(b"\xff\xd8" + b"1" * 187784 + b"\xff\xd9")
            (Path(temp_dir) / ".shot_29.jpg").write_bytes(b"x" * 190363)

            selected = vision.select_capture_frame(output, frames=30)

        self.assertIsNotNone(selected)
        self.assertEqual(selected.name, ".shot_28.jpg")

    def test_capture_jpeg_retries_when_batch_is_only_tiny_black_frames(self):
        vision = load_module()
        calls = []
        original_run = vision.subprocess.run

        with tempfile.TemporaryDirectory() as temp_dir:
            output = Path(temp_dir) / "shot.jpg"

            def fake_run(cmd, check, timeout):
                calls.append((cmd, check, timeout))
                size = 41109 if len(calls) == 1 else 187788
                (Path(temp_dir) / ".shot_29.jpg").write_bytes(b"\xff\xd8" + b"x" * (size - 4) + b"\xff\xd9")

            try:
                vision.subprocess.run = fake_run
                vision.capture_jpeg(
                    "/dev/video40",
                    output,
                    timeout=1.0,
                    frames=30,
                    retry_delay=0.0,
                )
            finally:
                vision.subprocess.run = original_run

            self.assertEqual(len(calls), 2)
            self.assertEqual(output.stat().st_size, 187788)

    def test_msc_explorer_close_center_uses_preview_keyword(self):
        vision = load_module()
        response = {
            "image_size": {"width": 1920, "height": 1080},
            "ocr": [
                {"text": "RK3588MSC (E:)", "center": [924, 363], "box": [872, 351, 976, 375]},
                {"text": "驱动器工具", "center": [818, 391], "box": [784, 382, 852, 400]},
                {"text": "选择要预览的文件", "center": [1438, 668], "box": [1392, 659, 1484, 677]},
            ],
        }

        self.assertEqual(vision.msc_explorer_close_center(response), (1556, 363))

    def test_wait_after_report_closes_msc_explorer_before_finish(self):
        vision = load_module()
        flow = FakeVisionFlow(
            [
                {
                    "image_size": {"width": 1920, "height": 1080},
                    "ocr": [
                        {"text": "RK3588MSC (E:)", "center": [924, 363], "box": [872, 351, 976, 375]},
                        {"text": "驱动器工具", "center": [818, 391], "box": [784, 382, 852, 400]},
                        {"text": "选择要预览的文件", "center": [1438, 668], "box": [1392, 659, 1484, 677]},
                    ],
                },
                {"ocr": [{"text": "新建患者", "center": [173, 226]}]},
            ]
        )

        asyncio.run(flow._impl.wait_and_close_msc_explorer_after_report(vision.time.monotonic()))

        self.assertEqual(flow.hid_output.clicks, [(1556, 363)])

    def test_detect_window_closes_msc_explorer_when_seen(self):
        vision = load_module()
        original_capture_jpeg = vision.capture_jpeg
        original_post_image = vision.post_image
        responses = [
            {
                "image_size": {"width": 1920, "height": 1080},
                "ocr": [
                    {"text": "RK3588MSC (E:)", "center": [924, 363], "box": [872, 351, 976, 375]},
                    {"text": "驱动器工具", "center": [818, 391], "box": [784, 382, 852, 400]},
                    {"text": "选择要预览的文件", "center": [1438, 668], "box": [1392, 659, 1484, 677]},
                ],
            },
            {"ocr": [{"text": "新建患者", "center": [173, 226]}]},
        ]
        capture_calls = []

        def fake_capture_jpeg(*args, **kwargs):
            capture_calls.append((args, kwargs))

        def fake_post_image(*args, **kwargs):
            if not responses:
                raise AssertionError("unexpected post_image call")
            return responses.pop(0)

        try:
            vision.capture_jpeg = fake_capture_jpeg
            vision.post_image = fake_post_image
            flow = vision.VisionFlow(
                SimpleNamespace(
                    enabled=True,
                    device="/dev/video40",
                    capture_format="bgr",
                    capture_width=1920,
                    capture_height=1080,
                    capture_framerate=60,
                    capture_frames=1,
                    capture_io_mode=0,
                    workdir="/tmp/test-vision",
                    icon_endpoint="http://127.0.0.1/icon",
                    window_endpoint="http://127.0.0.1/window",
                    software="人体成分分析仪",
                    wait_after_open=0.0,
                    wait_after_action=0.0,
                    wait_after_no_detection=5.0,
                    wait_after_start=0.0,
                    analysis_wait=0.0,
                    max_runtime=5.0,
                    timeout_seconds=1.0,
                    close_msc_popup_when_detected=True,
                ),
                FakeHidOutput(),
            )

            response = asyncio.run(flow.detect_window("probe.jpg"))
        finally:
            vision.capture_jpeg = original_capture_jpeg
            vision.post_image = original_post_image

        self.assertEqual(response["ocr"][0]["text"], "新建患者")
        self.assertEqual(flow.hid_output.clicks, [(1556, 363)])
        self.assertEqual(len(capture_calls), 2)

    def test_label_two_executes_form_and_continues_until_analysis_finish(self):
        task = {"eventClassList": [{"clickType": 0, "x": 100, "y": 443}], "patient": {"patient_id": "P1"}}
        flow = FakeVisionFlow(
            [
                {"ocr": []},
                {
                    "label": "0",
                    "ocr": [
                        {"text": "用户登录", "center": [42, 13]},
                        {"text": "用户名：", "center": [65, 150]},
                        {"text": "密码：", "center": [58, 193]},
                        {"text": "登录", "center": [110, 257]},
                    ],
                },
                {"label": "1", "ocr": [{"text": "未选择患者"}, {"text": "就绪"}, {"text": "新建患者", "center": [176, 227]}]},
                {"label": "2", "ocr": []},
                {"label": "1", "ocr": [{"text": "患者号"}, {"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}]},
                {"label": "3", "ocr": [{"text": "检查完成"}, {"text": "数据分析", "center": [400, 400]}]},
                {
                    "label": "4",
                    "ocr": [
                        {"text": "是否生成PDF报告？", "center": [260, 180]},
                        {"text": "是(Y)", "center": [210, 260]},
                        {"text": "是", "center": [220, 320]},
                    ],
                },
                {"label": "5", "ocr": [{"text": "检查报告已生成！"}, {"text": "确定", "center": [260, 260]}]},
                {"label": "1", "ocr": [{"text": "新建患者", "center": [176, 227]}]},
            ]
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "analysis_finished")
        self.assertEqual(flow.open_count, 1)
        self.assertEqual(flow.hid_output.forms, [task])
        self.assertEqual(flow.hid_output.clicks[0], (110, 257))
        self.assertEqual(flow.clicked_texts, ["新建患者", "开始检查", "数据分析", "是", "确定", "新建患者"])

    def test_windows_response_keeps_label_one_ready_logic(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "1",
                    "box": [103, 104, 806, 738],
                    "ocr": [{"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}],
                }
            ]
        }

        self.assertEqual(vision.decide_action(response), ("click_text", "开始检查"))
        self.assertEqual(vision.find_ocr_center(response, "开始检查"), (300, 300))

    def test_prepare_can_restart_from_existing_ready_patient(self):
        task = {"eventClassList": [{"clickType": 0, "x": 100, "y": 443}], "patient": {"patient_id": "P1"}}
        flow = FakeVisionFlow(
            [
                {
                    "label": "1",
                    "ocr": [
                        {"text": "当前患者"},
                        {"text": "患号：P265607：年龄1科"},
                        {"text": "就绪"},
                        {"text": "开始检查", "center": [300, 300]},
                        {"text": "新建患者", "center": [176, 227]},
                    ],
                },
                {"label": "2", "ocr": []},
                {"label": "1", "ocr": [{"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}]},
                {"label": "3", "ocr": [{"text": "检查完成"}, {"text": "数据分析", "center": [400, 400]}]},
                {"label": "4", "ocr": [{"text": "是否生成PDF报告？"}, {"text": "是", "center": [220, 320]}]},
                {"label": "5", "ocr": [{"text": "检查报告已生成！"}, {"text": "确定", "center": [260, 260]}]},
                {"label": "1", "ocr": [{"text": "新建患者", "center": [176, 227]}]},
            ]
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "analysis_finished")
        self.assertEqual(flow.hid_output.forms, [task])
        self.assertEqual(flow.clicked_texts, ["新建患者", "开始检查", "数据分析", "是", "确定", "新建患者"])

    def test_prepare_can_restart_from_completed_patient(self):
        task = {"eventClassList": [{"clickType": 0, "x": 100, "y": 443}], "patient": {"patient_id": "P1"}}
        flow = FakeVisionFlow(
            [
                {
                    "label": "1",
                    "ocr": [
                        {"text": "检查完成！"},
                        {"text": "开始检查", "center": [300, 300]},
                        {"text": "新建患者", "center": [176, 227]},
                    ],
                },
                {"label": "2", "ocr": []},
                {"label": "1", "ocr": [{"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}]},
                {"label": "3", "ocr": [{"text": "检查完成"}, {"text": "数据分析", "center": [400, 400]}]},
                {"label": "4", "ocr": [{"text": "是否生成PDF报告？"}, {"text": "是", "center": [220, 320]}]},
                {"label": "5", "ocr": [{"text": "检查报告已生成！"}, {"text": "确定", "center": [260, 260]}]},
                {"label": "1", "ocr": [{"text": "新建患者", "center": [176, 227]}]},
            ]
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "analysis_finished")
        self.assertEqual(flow.hid_output.forms, [task])
        self.assertEqual(flow.clicked_texts, ["新建患者", "开始检查", "数据分析", "是", "确定", "新建患者"])

    def test_prepare_clears_stale_pdf_prompt_before_new_patient(self):
        vision = load_module()
        task = {"eventClassList": [{"clickType": 0, "x": 100, "y": 443}], "patient": {"patient_id": "P1"}}
        yes_text = vision.YES_BUTTON_TEXTS[0]
        confirm_text = vision.CONFIRM_BUTTON_TEXTS[0]
        flow = FakeVisionFlow(
            [
                {
                    "ocr": [
                        {"text": "?PDFSN" + vision.PDF_REPORT_PROMPT_TEXT[-2:] + ")", "center": [948, 532]},
                        {"text": yes_text, "center": [924, 600]},
                        {"text": vision.NEW_PATIENT_TEXT, "center": [210, 252]},
                    ]
                },
                {"ocr": [{"text": vision.REPORT_GENERATED_TEXT}, {"text": confirm_text, "center": [260, 260]}]},
                {
                    "ocr": [
                        {"text": vision.READY_TEXT},
                        {"text": vision.START_CHECK_TEXT, "center": [300, 300]},
                        {"text": vision.NEW_PATIENT_TEXT, "center": [176, 227]},
                    ]
                },
                {"label": "2", "ocr": []},
                {"ocr": [{"text": vision.START_CHECK_TEXT, "center": [300, 300]}]},
                {"ocr": [{"text": vision.CHECK_DONE_TEXT}, {"text": vision.ANALYSIS_TEXT, "center": [400, 400]}]},
                {"ocr": [{"text": vision.PDF_REPORT_PROMPT_TEXT}, {"text": yes_text, "center": [220, 320]}]},
                {"ocr": [{"text": vision.REPORT_GENERATED_TEXT}, {"text": confirm_text, "center": [260, 260]}]},
                {"ocr": [{"text": vision.NEW_PATIENT_TEXT, "center": [176, 227]}]},
            ]
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "analysis_finished")
        self.assertEqual(flow.hid_output.forms, [task])
        self.assertEqual(
            flow.clicked_texts,
            [
                yes_text,
                confirm_text,
                vision.NEW_PATIENT_TEXT,
                vision.START_CHECK_TEXT,
                vision.ANALYSIS_TEXT,
                yes_text,
                confirm_text,
                vision.NEW_PATIENT_TEXT,
            ],
        )

    def test_label_zero_wins_over_label_one_when_both_are_detected(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "0",
                    "ocr": [
                        {"text": "用户登录", "center": [42, 13]},
                        {"text": "用户名：", "center": [65, 150]},
                        {"text": "密码：", "center": [58, 193]},
                        {"text": "登录", "center": [110, 259]},
                    ],
                },
                {
                    "label": "1",
                    "ocr": [
                        {"text": "登录", "center": [113, 257]},
                        {"text": "未选择患者", "center": [456, 557]},
                        {"text": "新建患者", "center": [176, 227]},
                    ],
                },
            ]
        }

        self.assertEqual(vision.decide_action(response), ("click_text", "登录"))

    def test_label_three_plus_label_four_prefers_confirm_dialog(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "3",
                    "box": [109, 106, 808, 745],
                    "ocr": [{"text": "检查完成"}, {"text": "数据分析", "center": [400, 400]}],
                },
                {"label": "4", "box": [0, 0, 391, 310], "ocr": [{"text": "是(Y)", "center": [500, 500]}]},
            ]
        }

        self.assertEqual(vision.decide_action(response), ("click_text", "是(Y)"))
        self.assertEqual(vision.find_ocr_center(response, "是(Y)"), (500, 500))

    def test_label_five_plus_label_three_prefers_confirm_dialog(self):
        vision = load_module()
        response = {
            "windows": [
                {"label": "5", "box": [0, 0, 391, 310], "ocr": [{"text": "确定", "center": [260, 260]}]},
                {
                    "label": "3",
                    "box": [109, 106, 808, 745],
                    "ocr": [{"text": "检查完成"}, {"text": "数据分析", "center": [400, 400]}],
                },
            ]
        }

        self.assertEqual(vision.decide_action(response), ("click_text", "确定"))
        self.assertEqual(vision.find_ocr_center(response, "确定"), (260, 260))

    def test_after_analysis_label_three_plus_label_four_prefers_confirm_dialog(self):
        vision = load_module()
        response = {
            "windows": [
                {"label": "3", "ocr": [{"text": "检查完成"}]},
                {"label": "4", "ocr": [{"text": "是(Y)", "center": [500, 500]}]},
            ]
        }

        self.assertEqual(vision.decide_after_analysis(response), ("click_text", "是(Y)"))

    def test_merged_label_four_uses_confirm_when_yes_is_absent(self):
        vision = load_module()
        response = {
            "windows": [
                {"label": "3", "ocr": [{"text": "检查完成"}]},
                {"label": "4", "ocr": [{"text": "确认", "center": [280, 260]}]},
            ]
        }

        self.assertEqual(vision.decide_after_analysis(response), ("click_text", "确认"))
        self.assertEqual(vision.find_ocr_center(response, "确认"), (280, 260))

    def test_pdf_report_label_four_chooses_lowest_yes_text(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "4",
                    "ocr": [
                        {"text": "是否生成PDF报告？", "center": [260, 180]},
                        {"text": "是(Y)", "center": [210, 260]},
                        {"text": "是", "center": [220, 320]},
                        {"text": "否(N)", "center": [310, 320]},
                    ],
                }
            ]
        }

        self.assertEqual(vision.decide_after_analysis(response), ("click_text", "是"))
        self.assertEqual(vision.find_ocr_center(response, "是"), (220, 320))

    def test_pdf_report_prompt_accepts_ocr_spaces(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "4",
                    "ocr": [
                        {"text": "是否生成 PDF 报告?", "center": [960, 524]},
                        {"text": "是(Y)", "center": [922, 602]},
                        {"text": "否(N)", "center": [1016, 602]},
                    ],
                }
            ]
        }

        self.assertTrue(vision.is_pdf_report_prompt(response))
        self.assertEqual(vision.pdf_report_yes_target(response), "是(Y)")

    def test_pdf_report_prompt_accepts_noisy_pdf_report_hint(self):
        vision = load_module()
        response = {
            "ocr": [
                {"text": "?PDFSN" + vision.PDF_REPORT_PROMPT_TEXT[-2:] + ")", "center": [948, 532]},
                {"text": vision.YES_BUTTON_TEXTS[0], "center": [926, 600]},
            ]
        }

        self.assertTrue(vision.is_pdf_report_prompt(response))
        self.assertEqual(vision.pdf_report_yes_target(response), vision.YES_BUTTON_TEXTS[0])

    def test_report_generated_accepts_label_one_with_confirm_text(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "1",
                    "ocr": [
                        {"text": "检查报告已生成！", "center": [893, 516]},
                        {"text": "确定", "center": [1073, 610]},
                    ],
                }
            ]
        }

        self.assertTrue(vision.is_report_generated(response))

    def test_report_generated_accepts_visual_info_dialog_when_ocr_misses_text(self):
        vision = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "report_done.jpg"
            image = Image.new("RGB", (1920, 1080), (235, 235, 235))
            draw = ImageDraw.Draw(image)
            draw.ellipse((800, 505, 840, 545), fill=(0, 120, 215))
            draw.rectangle((1030, 596, 1118, 624), outline=(0, 120, 215), width=3)
            image.save(image_path)

            response = {"_image_path": str(image_path), "ocr": []}

            self.assertTrue(vision.is_report_generated(response))
            self.assertEqual(vision.report_generated_visual_confirm_center(str(image_path)), (1075, 610))

    def test_report_generated_visual_dialog_rejects_plain_blue_background(self):
        vision = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "desktop.jpg"
            image = Image.new("RGB", (1920, 1080), (0, 120, 215))
            image.save(image_path)

            self.assertIsNone(vision.report_generated_visual_confirm_center(str(image_path)))

    def test_generic_window_label_uses_ocr_for_login(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "1",
                    "ocr": [
                        {"text": "用户登录", "center": [42, 13]},
                        {"text": "用户名：", "center": [65, 150]},
                        {"text": "密码：", "center": [58, 193]},
                        {"text": "登录", "center": [110, 259]},
                    ],
                }
            ]
        }

        self.assertTrue(vision.is_login_window(response))
        self.assertEqual(vision.decide_action(response), ("click_text", "登录"))

    def test_login_title_only_uses_fixed_button_offset(self):
        vision = load_module()
        response = {"ocr": [{"text": vision.LOGIN_TITLE_TEXT, "center": [42, 13]}]}

        self.assertTrue(vision.is_login_window(response))
        self.assertEqual(vision.login_button_center(response), (110, 258))

    def test_concatenated_toolbar_text_estimates_button_centers(self):
        vision = load_module()
        response = {
            "ocr": [
                {
                    "text": "新建患者开始检查数据分析存储目录",
                    "box": [92, 162, 452, 186],
                    "center": [272, 174],
                }
            ]
        }

        self.assertEqual(vision.find_action_center(response, vision.NEW_PATIENT_TEXT), (137, 174))
        self.assertEqual(vision.find_action_center(response, vision.START_CHECK_TEXT), (227, 174))
        self.assertEqual(vision.find_action_center(response, vision.ANALYSIS_TEXT), (317, 174))
        self.assertTrue(vision.is_ready_to_create_patient(response))

    def test_generic_window_label_uses_ocr_for_new_patient_dialog(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "0",
                    "ocr": [
                        {"text": "新建患者", "center": [48, 13]},
                        {"text": "患者号:", "center": [45, 58]},
                        {"text": "姓名：", "center": [39, 121]},
                        {"text": "性别:", "center": [39, 186]},
                        {"text": "年龄：", "center": [39, 250]},
                        {"text": "确认", "center": [99, 443]},
                    ],
                }
            ]
        }

        self.assertTrue(vision.is_new_patient_window(response))
        self.assertEqual(vision.decide_action(response), ("form_input", None))

    def test_partial_new_patient_title_still_starts_form_input(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "ocr": [
                        {"text": "新建患", "center": [46, 12]},
                        {"text": "患者号", "center": [44, 57]},
                    ],
                }
            ]
        }

        self.assertTrue(vision.is_new_patient_window(response))
        self.assertEqual(vision.decide_action(response), ("form_input", None))

    def test_order_department_window_starts_form_before_main_window_actions(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "0",
                    "ocr": [
                        {"text": "当前患者", "center": [159, 523]},
                        {"text": "未选择患者", "center": [456, 557]},
                        {"text": "检查进度", "center": [159, 599]},
                        {"text": "就绪", "center": [456, 664]},
                        {"text": "新建患者", "center": [176, 227]},
                    ],
                },
                {
                    "label": "0",
                    "ocr": [
                        {"text": "患者号:", "center": [45, 58]},
                        {"text": "开单科室：", "center": [50, 313]},
                        {"text": "确认", "center": [99, 443]},
                    ],
                },
            ]
        }

        self.assertTrue(vision.is_new_patient_window(response))
        self.assertEqual(vision.decide_action(response), ("form_input", None))

    def test_generic_window_label_uses_ocr_for_pdf_prompt(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "1",
                    "ocr": [
                        {"text": "选择报告类型", "center": [902, 474]},
                        {"text": "是否生成 PDF 报告?", "center": [960, 524]},
                        {"text": "是(Y)", "center": [922, 602]},
                        {"text": "否(N)", "center": [1016, 602]},
                    ],
                }
            ]
        }

        self.assertTrue(vision.is_pdf_report_prompt(response))
        self.assertEqual(vision.decide_after_analysis(response), ("click_text", "是(Y)"))

    def test_generic_window_label_uses_ocr_for_report_generated(self):
        vision = load_module()
        response = {
            "windows": [
                {
                    "label": "1",
                    "ocr": [
                        {"text": "分析完成", "center": [834, 467]},
                        {"text": "检查报告已生成！", "center": [893, 516]},
                        {"text": "确定", "center": [1073, 610]},
                    ],
                }
            ]
        }

        self.assertTrue(vision.is_report_generated(response))
        self.assertEqual(vision.decide_after_analysis(response), ("click_text", "确定"))

    def test_start_check_waits_until_hid_form_input_has_completed(self):
        task = {"eventClassList": [{"clickType": 0, "x": 100, "y": 443}], "patient": {"patient_id": "P1"}}
        flow = FakeVisionFlow(
            [
                {"label": "1", "ocr": [{"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}]},
                {"label": "2", "ocr": []},
                {"label": "1", "ocr": [{"text": "患者号"}, {"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}]},
                {"label": "3", "ocr": [{"text": "检查完成"}, {"text": "数据分析", "center": [400, 400]}]},
                {
                    "label": "4",
                    "ocr": [{"text": "是否生成PDF报告？"}, {"text": "是", "center": [220, 320]}],
                },
                {"label": "5", "ocr": [{"text": "检查报告已生成！"}, {"text": "确定", "center": [260, 260]}]},
                {"label": "1", "ocr": [{"text": "新建患者", "center": [176, 227]}]},
            ]
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "analysis_finished")
        self.assertEqual(flow.hid_output.forms, [task])
        self.assertEqual(flow.clicked_texts, ["开始检查", "数据分析", "是", "确定", "新建患者"])

    def test_start_check_uses_ocr_even_when_label_is_three(self):
        vision = load_module()
        response = {"label": "3", "ocr": [{"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}]}

        self.assertEqual(vision.decide_action(response), ("click_text", "开始检查"))

    def test_start_check_ready_does_not_require_exact_patient_id_ocr(self):
        vision = load_module()
        response = {
            "label": "1",
            "ocr": [
                {"text": "患号：P265607：年龄1科"},
                {"text": "就绪"},
                {"text": "开始检查", "center": [300, 300]},
            ],
        }

        self.assertTrue(vision.is_ready_to_start_check(response))

    def test_start_check_ready_accepts_concatenated_toolbar_without_ready_text(self):
        vision = load_module()
        response = {
            "label": "1",
            "ocr": [
                {
                    "text": vision.NEW_PATIENT_TEXT + vision.START_CHECK_TEXT + vision.ANALYSIS_TEXT,
                    "box": [164, 238, 528, 265],
                    "center": [346, 252],
                }
            ],
        }

        self.assertTrue(vision.is_ready_to_start_check(response))

    def test_start_check_ready_rejects_unselected_patient_state(self):
        vision = load_module()
        response = {
            "label": "1",
            "ocr": [
                {"text": vision.UNSELECTED_PATIENT_TEXT},
                {"text": vision.START_CHECK_TEXT, "center": [300, 300]},
            ],
        }

        self.assertFalse(vision.is_ready_to_start_check(response))

    def test_check_complete_uses_ocr_even_when_label_is_one(self):
        vision = load_module()
        response = {"label": "1", "ocr": [{"text": "检查完成！"}, {"text": "数据分析", "center": [400, 400]}]}

        self.assertEqual(vision.decide_action(response), ("analysis", None))

    def test_check_complete_accepts_full_green_progress_bar_when_ocr_misses_done_text(self):
        vision = load_module()
        with tempfile.TemporaryDirectory() as tmpdir:
            image_path = Path(tmpdir) / "complete.jpg"
            image = Image.new("RGB", (1920, 1080), (235, 235, 235))
            draw = ImageDraw.Draw(image)
            draw.rectangle((174, 650, 792, 670), fill=(0, 180, 20))
            image.save(image_path)

            response = {
                "_image_path": str(image_path),
                "ocr": [{"text": vision.ANALYSIS_TEXT, "center": [390, 252]}],
            }

            self.assertTrue(vision.is_check_complete(response))

    def test_linear_flow_starts_directly_from_label_two(self):
        task = {"eventClassList": [{"clickType": 0, "x": 100, "y": 443}], "patient": {"patient_id": "P1"}}
        flow = FakeVisionFlow(
            [
                {"label": "2", "ocr": []},
                {"label": "1", "ocr": [{"text": "患者号"}, {"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}]},
                {"label": "1", "ocr": [{"text": "检查完成！"}, {"text": "数据分析", "center": [400, 400]}]},
                {
                    "label": "4",
                    "ocr": [{"text": "是否生成PDF报告？"}, {"text": "是", "center": [220, 320]}],
                },
                {"label": "5", "ocr": [{"text": "检查报告已生成！"}, {"text": "确定", "center": [260, 260]}]},
                {"label": "1", "ocr": [{"text": "新建患者", "center": [176, 227]}]},
            ]
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "analysis_finished")
        self.assertEqual(flow.hid_output.forms, [task])
        self.assertEqual(flow.open_count, 0)
        self.assertEqual(flow.clicked_texts, ["开始检查", "数据分析", "是", "确定", "新建患者"])

    def test_linear_flow_starts_directly_from_generic_label_zero_order_department_form(self):
        task = {"eventClassList": [{"clickType": 0, "x": 100, "y": 443}], "patient": {"patient_id": "P1"}}
        flow = FakeVisionFlow(
            [
                {
                    "label": "0",
                    "ocr": [
                        {"text": "患者号:", "center": [45, 58]},
                        {"text": "开单科室：", "center": [50, 313]},
                        {"text": "确认", "center": [99, 443]},
                    ],
                },
                {"label": "0", "ocr": [{"text": "患者号"}, {"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}]},
                {"label": "0", "ocr": [{"text": "检查完成！"}, {"text": "数据分析", "center": [400, 400]}]},
                {
                    "label": "0",
                    "ocr": [{"text": "是否生成PDF报告？"}, {"text": "是", "center": [220, 320]}],
                },
                {"label": "0", "ocr": [{"text": "检查报告已生成！"}, {"text": "确定", "center": [260, 260]}]},
                {"label": "0", "ocr": [{"text": "新建患者", "center": [176, 227]}]},
            ]
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "analysis_finished")
        self.assertEqual(flow.hid_output.forms, [task])
        self.assertEqual(flow.open_count, 0)
        self.assertEqual(flow.clicked_texts, ["开始检查", "数据分析", "是", "确定", "新建患者"])

    def test_icon_not_found_waits_longer_and_retries(self):
        task = {"eventClassList": [], "patient": {"patient_id": "P1"}}
        flow = FakeVisionFlow(
            [
                {"ocr": []},
                {"ocr": []},
                {"label": "2", "ocr": []},
                {"label": "1", "ocr": [{"text": "患者号"}, {"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}]},
                {"label": "3", "ocr": [{"text": "检查完成"}, {"text": "数据分析", "center": [400, 400]}]},
                {"label": "4", "ocr": [{"text": "是否生成PDF报告？"}, {"text": "是", "center": [220, 320]}]},
                {"label": "5", "ocr": [{"text": "检查报告已生成！"}, {"text": "确定", "center": [260, 260]}]},
                {"label": "1", "ocr": [{"text": "新建患者", "center": [176, 227]}]},
            ],
            open_results=[False, True],
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "analysis_finished")
        self.assertEqual(flow.open_count, 2)
        self.assertIn(5.0, flow.sleeps)
        self.assertEqual(flow.hid_output.forms, [task])

    def test_empty_detection_after_successful_open_waits_instead_of_opening_again(self):
        task = {"eventClassList": [], "patient": {"patient_id": "P1"}}
        flow = FakeVisionFlow(
            [
                {"ocr": []},
                {
                    "label": "0",
                    "ocr": [
                        {"text": "用户登录", "center": [42, 13]},
                        {"text": "用户名：", "center": [65, 150]},
                        {"text": "密码：", "center": [58, 193]},
                        {"text": "登录", "center": [110, 257]},
                    ],
                },
                {"ocr": []},
                {"label": "1", "ocr": [{"text": "未选择患者"}, {"text": "就绪"}, {"text": "新建患者", "center": [176, 227]}]},
                {"label": "2", "ocr": []},
                {"label": "1", "ocr": [{"text": "患者号"}, {"text": "就绪"}, {"text": "开始检查", "center": [300, 300]}]},
                {"label": "3", "ocr": [{"text": "检查完成"}, {"text": "数据分析", "center": [400, 400]}]},
                {"label": "4", "ocr": [{"text": "是否生成PDF报告？"}, {"text": "是", "center": [220, 320]}]},
                {"label": "5", "ocr": [{"text": "检查报告已生成！"}, {"text": "确定", "center": [260, 260]}]},
                {"label": "1", "ocr": [{"text": "新建患者", "center": [176, 227]}]},
            ],
            open_results=[True, True],
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "analysis_finished")
        self.assertEqual(flow.open_count, 1)
        self.assertEqual(flow.hid_output.clicks[0], (110, 257))
        self.assertEqual(flow.clicked_texts, ["新建患者", "开始检查", "数据分析", "是", "确定", "新建患者"])

    def test_bodypass_flow_opens_inputs_member_and_prints_result(self):
        task = {
            "scan_text": "P2605260007",
            "eventClassList": [],
            "patient": {"patient_id": "P2605260007", "patient_name": "张三"},
        }
        main = {
            "windows": [
                {
                    "label": "0",
                    "box": [467, 166, 1479, 895],
                    "ocr": [
                        {"text": "人体成分数据管理程序（BodyPass）", "center": [575, 186]},
                        {"text": "编号", "center": [505, 362], "box": [488, 353, 523, 372]},
                        {"text": "姓名", "center": [505, 390], "box": [488, 379, 523, 401]},
                    ],
                }
            ]
        }
        result_ready = {
            "ocr": [
                {"text": "Machine State=显示检测结果", "center": [616, 598]},
            ]
        }
        flow = FakeVisionFlow(
            [
                {"ocr": []},
                main,
                result_ready,
                {"ocr": [{"text": "检测结果明细", "center": [550, 180]}, {"text": "预览检测结果", "center": [900, 800]}]},
                {"ocr": [{"text": "人体成分分析报告", "center": [778, 363]}]},
                {
                    "windows": [
                        {
                            "box": [473, 152, 1435, 938],
                            "ocr": [
                                {"text": "选择打印机", "center": [545, 457]},
                                {"text": "打印（P)", "center": [866, 873]},
                                {"text": "取消", "center": [963, 872]},
                            ],
                        }
                    ]
                },
                {"ocr": [{"text": "人体成分分析报告", "center": [778, 363]}]},
                {"ocr": [{"text": "检测结果明细", "center": [550, 180]}]},
            ],
            flow="bodypass",
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "bodypass_finished")
        self.assertEqual(flow.open_count, 1)
        self.assertEqual(
            flow.hid_output.inputs,
            [
                ("P2605260007", 685, 362, "bodypass_patient_id"),
                ("张三", 685, 390, "bodypass_patient_name"),
            ],
        )
        self.assertEqual(
            flow.hid_output.clicks,
            [
                (1287, 260),
                (1037, 260),
                (900, 800),
                (1257, 244),
                (866, 873),
                (1390, 244),
                (1387, 356),
            ],
        )

    def test_oct_flow_opens_icon_then_runs_hid_template_without_window_detection(self):
        task = {"scan_text": "P2605260007", "eventClassList": [{"clickType": 0, "x": 100, "y": 200}]}
        flow = FakeVisionFlow([], open_results=[True], flow="oct")

        started = []
        result = asyncio.run(flow._impl.run_until_form_done(task, on_hid_start=lambda: started.append(True)))

        self.assertEqual(result, "form_done")
        self.assertEqual(flow.open_count, 1)
        self.assertEqual(started, [True])
        self.assertEqual(flow.hid_output.forms, [task])
        self.assertEqual(flow.clicked_texts, [])

    def test_oct_flow_assumes_foreground_when_icon_is_not_visible(self):
        task = {"scan_text": "P2605260007", "eventClassList": []}
        flow = FakeVisionFlow([], open_results=[False], flow="oct")

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "form_done")
        self.assertEqual(flow.open_count, 1)
        self.assertEqual(flow.sleeps, [])
        self.assertEqual(flow.hid_output.forms, [task])

    def test_pelvic_floor_flow_opens_logs_in_creates_patient_and_saves(self):
        task = {"scan_text": "P2605260007", "eventClassList": [{"clickType": 0, "x": 818, "y": 965}]}
        flow = FakeVisionFlow([], open_results=[True], flow="pelvic_floor")
        started = []

        result = asyncio.run(flow._impl.run_until_form_done(task, on_hid_start=lambda: started.append(True)))

        self.assertEqual(result, "pelvic_floor_finished")
        self.assertEqual(flow.open_count, 1)
        self.assertEqual(started, [True])
        self.assertEqual(flow.hid_output.forms, [task])
        self.assertEqual(
            flow.hid_output.clicks,
            [(488, 765), (509, 706), (72, 48), (1450, 426), (791, 531)],
        )

    def test_pelvic_floor_flow_assumes_foreground_when_icon_not_visible(self):
        task = {"scan_text": "P2605260007", "eventClassList": []}
        flow = FakeVisionFlow([], open_results=[False], flow="pelvic_floor")

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "pelvic_floor_finished")
        self.assertEqual(flow.open_count, 1)
        self.assertEqual(flow.hid_output.forms, [task])
        self.assertEqual(
            flow.hid_output.clicks,
            [(115, 934), (72, 48), (1450, 426), (791, 531)],
        )

    def test_esophageal_flow_opens_app_prepares_patient_and_saves(self):
        task = {"scan_text": "P2605260007", "eventClassList": [{"clickType": 0, "x": 167, "y": 481}]}
        flow = FakeVisionFlow(
            [
                {"ocr": []},
                {
                    "windows": [
                        {
                            "box": [17, 8, 1041, 819],
                            "ocr": [{"text": "\u98df\u9053\u52a8\u529b\u91c7\u96c6\u7cfb\u7edf V.2.1", "center": [162, 23]}],
                        }
                    ]
                },
                {"ocr": [{"text": "\u70b9\u51fb \"Start\" \u8f93\u5165\u75c5\u4eba\u4fe1\u606f"}]},
                {"ocr": [{"text": "\u70b9\u51fb \"End\" \u7ed3\u675f\u68c0\u6d4b\u5e76\u5f00\u59cb\u65b0\u60a3\u8005"}]},
            ],
            open_results=[True],
            flow="esophageal_motility",
        )
        started = []

        result = asyncio.run(flow._impl.run_until_form_done(task, on_hid_start=lambda: started.append(True)))

        self.assertEqual(result, "esophageal_motility_finished")
        self.assertEqual(flow.open_count, 1)
        self.assertEqual(started, [True])
        self.assertEqual(flow.hid_output.forms[0]["eventClassList"][0]["x"], 184)
        self.assertEqual(flow.hid_output.forms[0]["eventClassList"][0]["y"], 489)
        self.assertEqual(
            flow.hid_output.clicks,
            [
                (395, 604),
                (496, 472),
                (62, 84),
                (1002, 232),
                (1002, 232),
                (1002, 232),
                (1002, 232),
                (347, 485),
                (807, 567),
                (62, 84),
            ],
        )
        self.assertIn(0.5, flow.sleeps)
        self.assertIn(0.7, flow.sleeps)

    def test_esophageal_flow_uses_already_open_patient_form(self):
        task = {"scan_text": "P2605260007", "eventClassList": []}
        flow = FakeVisionFlow(
            [
                {
                    "windows": [
                        {
                            "box": [17, 8, 1041, 819],
                            "ocr": [
                                {"text": "\u98df\u9053\u52a8\u529b\u91c7\u96c6\u7cfb\u7edf V.2.1", "center": [162, 23]},
                                {"text": "\u60a3\u8005\u4fe1\u606f", "center": [489, 222]},
                                {"text": "\u60a3\u8005 ID", "center": [169, 379]},
                                {"text": "\u6027\u522b", "center": [146, 439]},
                            ],
                        }
                    ]
                },
                {"ocr": [{"text": "\u70b9\u51fb \"End\" \u7ed3\u675f\u68c0\u6d4b\u5e76\u5f00\u59cb\u65b0\u60a3\u8005"}]},
            ],
            flow="esophageal_motility",
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "esophageal_motility_finished")
        self.assertEqual(flow.open_count, 0)
        self.assertEqual(flow.hid_output.forms, [{"scan_text": "P2605260007", "eventClassList": []}])
        self.assertEqual(
            flow.hid_output.clicks,
            [
                (1002, 232),
                (1002, 232),
                (1002, 232),
                (1002, 232),
                (347, 485),
                (807, 567),
                (62, 84),
            ],
        )

    def test_esophageal_flow_treats_start_prompt_as_foreground_when_title_is_missed(self):
        task = {"scan_text": "P2605260007", "eventClassList": []}
        flow = FakeVisionFlow(
            [
                {
                    "windows": [
                        {
                            "box": [0, 0, 1920, 1080],
                            "ocr": [{"text": "点击 \"Start\" 输入病人信息", "center": [718, 129]}],
                        }
                    ]
                },
                {"ocr": [{"text": "\u70b9\u51fb \"End\" \u7ed3\u675f\u68c0\u6d4b\u5e76\u5f00\u59cb\u65b0\u60a3\u8005"}]},
            ],
            open_results=[False],
            flow="esophageal_motility",
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "esophageal_motility_finished")
        self.assertEqual(flow.open_count, 0)
        self.assertEqual(flow.hid_output.forms, [{"scan_text": "P2605260007", "eventClassList": []}])
        self.assertEqual(flow.hid_output.clicks[0], (62, 84))
        self.assertEqual(flow.hid_output.clicks[1:5], [(1002, 232), (1002, 232), (1002, 232), (1002, 232)])

    def test_esophageal_flow_infers_moved_window_anchor_from_start_prompt(self):
        task = {"scan_text": "P2605260007", "eventClassList": [{"clickType": 0, "x": 167, "y": 481}]}
        flow = FakeVisionFlow(
            [
                {
                    "windows": [
                        {
                            "box": [0, 0, 1920, 1080],
                            "ocr": [{"text": "点击 \"Start\" 输入病人信息", "center": [785, 214]}],
                        }
                    ]
                },
                {"ocr": [{"text": "\u70b9\u51fb \"End\" \u7ed3\u675f\u68c0\u6d4b\u5e76\u5f00\u59cb\u65b0\u60a3\u8005"}]},
            ],
            flow="esophageal_motility",
        )

        result = asyncio.run(flow.run_until_form_done(task))

        self.assertEqual(result, "esophageal_motility_finished")
        self.assertEqual(flow.open_count, 0)
        self.assertEqual(flow.hid_output.forms[0]["eventClassList"][0]["x"], 251)
        self.assertEqual(flow.hid_output.forms[0]["eventClassList"][0]["y"], 574)
        self.assertEqual(flow.hid_output.clicks[0], (129, 169))
        self.assertEqual(flow.hid_output.clicks[1:5], [(1069, 317), (1069, 317), (1069, 317), (1069, 317)])


if __name__ == "__main__":
    unittest.main()
