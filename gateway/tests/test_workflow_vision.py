import asyncio
import sys
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

aiohttp_stub = ModuleType("aiohttp")
aiohttp_stub.ClientSession = object
aiohttp_stub.ClientTimeout = lambda **kwargs: None
sys.modules.setdefault("aiohttp", aiohttp_stub)

yaml_stub = ModuleType("yaml")
yaml_stub.safe_load = lambda handle: {}
sys.modules.setdefault("yaml", yaml_stub)

import rk3588_gateway.workflow as workflow_module


GatewayWorkflow = workflow_module.GatewayWorkflow


class FakeQueue:
    def put(self, event):
        return None


class FakeHidOutput:
    def __init__(self):
        self.forms = []
        self.configured_actions = []

    async def execute_form(self, task):
        self.forms.append(task)

    async def execute_actions(self, actions, patient, wait_for_text=None):
        self.configured_actions.append((actions, patient, wait_for_text))


class FakeVisionFlow:
    def __init__(self):
        self.tasks = []

    async def run_until_form_done(self, task, on_hid_start=None):
        if on_hid_start:
            on_hid_start()
        self.tasks.append(task)
        return "analysis_finished"


class FakePatientApi:
    def __init__(self, records):
        self.records = list(records)

    async def query_records(self, scan):
        return list(self.records)


def make_config(vision_enabled=True):
    return SimpleNamespace(
        device=SimpleNamespace(id="dev1", type="人体成分检查"),
        patient_api=SimpleNamespace(enabled=False, endpoint="", timeout_seconds=1, user_agent="test", raw_dir="/tmp"),
        hid_input=SimpleNamespace(
            enabled=True,
            keyboard_backend="usb_gadget",
            mouse_backend="usb_gadget",
            keyboard_device="/dev/hidg0",
            mouse_device="/dev/hidg1",
            ch9350_serial_device="",
            ch9350_baudrate=115200,
            ch9350_state=0,
            ch9350_set_state2=False,
            ch9350_caps_led_mask=1,
            ch9350_mouse_frame="absolute7",
            ch9350_mouse_reset_to_origin=False,
            template_path="/tmp/template.json",
            screen_width=1920,
            screen_height=1080,
            action_delay_ms=1,
            start_delay_ms=1,
            force_caps_ascii=True,
            non_ascii_mode="powershell",
            powershell_wait_ms=1,
        ),
        vision=SimpleNamespace(
            enabled=vision_enabled,
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
        ),
    )


class WorkflowVisionTest(unittest.TestCase):
    def test_execute_input_task_uses_vision_flow_when_enabled(self):
        workflow = GatewayWorkflow(make_config(True), FakeQueue())
        workflow.hid_output = FakeHidOutput()
        workflow.vision_flow = FakeVisionFlow()
        task = {"eventClassList": [], "patient": {"patient_id": "P1"}}

        started = []
        result = asyncio.run(workflow._execute_input_task(task, on_hid_start=lambda: started.append(True)))

        self.assertEqual(result, "analysis_finished")
        self.assertEqual(workflow.vision_flow.tasks, [task])
        self.assertEqual(workflow.hid_output.forms, [])
        self.assertEqual(started, [True])

    def test_fixed_absolute_patient_entry_bypasses_vision_flow(self):
        workflow = GatewayWorkflow(make_config(True), FakeQueue())
        workflow.hid_output = FakeHidOutput()
        workflow.vision_flow = FakeVisionFlow()
        patient = {"patient_id": "P1", "patient_name": "Alice"}
        actions = [{"type": "input_field", "field": "patient_id", "x": 682, "y": 362}]

        asyncio.run(
            workflow.execute_patient_entry(
                "P1",
                patient,
                {"hid": {"coordinate_mode": "fixed_absolute", "actions": actions}},
            )
        )

        self.assertEqual(workflow.hid_output.forms, [])
        self.assertEqual(workflow.vision_flow.tasks, [])
        self.assertEqual(workflow.hid_output.configured_actions, [(actions, patient, None)])

    def test_fixed_absolute_patient_entry_rejects_empty_actions(self):
        workflow = GatewayWorkflow(make_config(True), FakeQueue())
        workflow.hid_output = FakeHidOutput()
        workflow.vision_flow = FakeVisionFlow()

        with self.assertRaisesRegex(RuntimeError, "requires configured actions"):
            asyncio.run(
                workflow.execute_patient_entry(
                    "P1",
                    {"patient_id": "P1"},
                    {"hid": {"coordinate_mode": "fixed_absolute", "actions": []}},
                )
            )

        self.assertEqual(workflow.vision_flow.tasks, [])

    def test_rk3588_scan_auto_selects_first_record_without_gpio_selection(self):
        workflow = GatewayWorkflow(make_config(True), FakeQueue())
        workflow.hid_output = FakeHidOutput()
        workflow.vision_flow = FakeVisionFlow()
        workflow.patient_api = FakePatientApi(
            [
                {"patient_id": "P-first", "patient_name": "Alice", "exam_item": "Other A"},
                {"patient_id": "P-second", "patient_name": "Bob", "exam_item": "Other B"},
            ]
        )
        original_build_form_task = workflow_module.build_form_task

        def fake_build_form_task(scan, record, template_path):
            return {"patient": {"patient_id": record["patient_id"]}, "eventClassList": []}

        try:
            workflow_module.build_form_task = fake_build_form_task
            asyncio.run(workflow.handle_scan("P2605260007"))
        finally:
            workflow_module.build_form_task = original_build_form_task

        self.assertEqual(workflow.vision_flow.tasks[0]["patient"]["patient_id"], "P-first")
        self.assertEqual(workflow.display_state["selected_index"], 0)


if __name__ == "__main__":
    unittest.main()
