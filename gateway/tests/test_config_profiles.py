import sys
import json
import tempfile
import unittest
from pathlib import Path
from types import ModuleType


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

yaml_stub = ModuleType("yaml")
yaml_stub.safe_load = json.load
sys.modules.setdefault("yaml", yaml_stub)

from rk3588_gateway.config import load_config


def base_config():
    return {
        "device": {"id": "dev1", "location": "bench", "type": "人体成分检查", "profile_dir": "/tmp/device"},
        "scanner": {"enabled": False},
        "patient_api": {"enabled": False},
        "hid_input": {"enabled": True},
        "vision": {
            "enabled": True,
            "software": "人体成分分析仪",
            "icon_endpoint": "http://127.0.0.1/icon",
            "window_endpoint": "http://127.0.0.1/window",
        },
        "printer": {"enabled": False},
        "print_capture": {"enabled": False},
        "vm_transfer": {"enabled": False},
        "uploader": {"enabled": False},
        "local_api": {"enabled": True},
        "storage": {"sqlite_path": "/tmp/events.db"},
        "logging": {"level": "INFO"},
    }


def write_yaml(path, payload):
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


class ConfigProfilesTest(unittest.TestCase):
    def test_bodypass_fixed_hid_only_enters_patient_information(self):
        profile_path = Path(__file__).resolve().parents[1] / "profiles" / "bodypass_fixed_hid.json"
        profile = json.loads(profile_path.read_text(encoding="utf-8"))
        actions = profile["actions"]

        self.assertEqual(len(actions), 6)
        self.assertEqual(actions[-1]["type"], "input_field")
        self.assertEqual(actions[-1]["field"], "birthday")
        self.assertFalse(
            any(
                action.get("type") == "click"
                and action.get("x") == 739
                and action.get("y") == 570
                for action in actions
            )
        )

    def test_legacy_config_without_profile_still_uses_device_and_vision_values(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            payload = base_config()
            write_yaml(config_path, payload)

            config = load_config(config_path)

        self.assertEqual(config.active_profile, "")
        self.assertEqual(config.device.type, "人体成分检查")
        self.assertEqual(config.vision.software, "人体成分分析仪")
        self.assertEqual(config.vision.flow, "body_composition")
        self.assertEqual(config.vision.frame_endpoint, "")

    def test_vision_frame_endpoint_can_be_configured(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            config_path = Path(temp_dir) / "config.yaml"
            payload = base_config()
            payload["vision"]["frame_endpoint"] = "http://127.0.0.1:8090/api/frame.jpg?quality=90"
            write_yaml(config_path, payload)

            config = load_config(config_path)

        self.assertEqual(config.vision.frame_endpoint, "http://127.0.0.1:8090/api/frame.jpg?quality=90")

    def test_active_profile_file_overrides_device_type_and_software(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            profiles_dir = root / "profiles"
            profiles_dir.mkdir()
            write_yaml(
                profiles_dir / "body_composition.yaml",
                {
                    "id": "body_composition",
                    "device_type": "人体成分检查",
                    "software": "人体成分分析仪",
                    "flow": "body_composition",
                    "vision": {"close_msc_popup_when_detected": True, "wait_after_open": 3.0},
                },
            )
            payload = base_config()
            payload["device"]["type"] = "另一个检查"
            payload["vision"]["software"] = "另一个软件"
            payload["vision"]["wait_after_open"] = 1.0
            payload["active_profile"] = "body_composition"
            payload["profile_files"] = ["profiles/body_composition.yaml"]
            config_path = root / "config.yaml"
            write_yaml(config_path, payload)

            config = load_config(config_path)

        self.assertEqual(config.active_profile, "body_composition")
        self.assertEqual(config.device.type, "人体成分检查")
        self.assertEqual(config.vision.software, "人体成分分析仪")
        self.assertEqual(config.vision.flow, "body_composition")
        self.assertTrue(config.vision.close_msc_popup_when_detected)
        self.assertEqual(config.vision.wait_after_open, 3.0)

    def test_active_inline_profile_can_switch_to_another_software(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = base_config()
            payload["active_profile"] = "other"
            payload["profiles"] = {
                "other": {
                    "device_type": "另一个检查",
                    "software": "另一个软件",
                    "flow": "other_flow",
                    "vision": {"close_msc_popup_when_detected": False},
                }
            }
            config_path = Path(temp_dir) / "config.yaml"
            write_yaml(config_path, payload)

            config = load_config(config_path)

        self.assertEqual(config.device.type, "另一个检查")
        self.assertEqual(config.vision.software, "另一个软件")
        self.assertEqual(config.vision.flow, "other_flow")
        self.assertFalse(config.vision.close_msc_popup_when_detected)

    def test_active_profile_can_use_icon_vision_and_override_hid_template(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = base_config()
            payload["active_profile"] = "oct"
            payload["profiles"] = {
                "oct": {
                    "device_type": "OCT",
                    "software": "OCT",
                    "flow": "oct",
                    "hid_input": {
                        "template_path": "/opt/rk3588_gateway/MarkInfo_OCT_Config_100.json",
                        "action_delay_ms": 80,
                    },
                    "vision": {
                        "enabled": True,
                        "close_msc_popup_when_detected": False,
                    },
                }
            }
            config_path = Path(temp_dir) / "config.yaml"
            write_yaml(config_path, payload)

            config = load_config(config_path)

        self.assertEqual(config.device.type, "OCT")
        self.assertEqual(config.hid_input.template_path, "/opt/rk3588_gateway/MarkInfo_OCT_Config_100.json")
        self.assertEqual(config.hid_input.action_delay_ms, 80)
        self.assertTrue(config.vision.enabled)
        self.assertEqual(config.vision.software, "OCT")
        self.assertEqual(config.vision.flow, "oct")

    def test_active_profile_can_switch_to_pelvic_floor(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = base_config()
            payload["active_profile"] = "pelvic_floor"
            payload["profiles"] = {
                "pelvic_floor": {
                    "device_type": "盆底肌",
                    "software": "盆底肌",
                    "flow": "pelvic_floor",
                    "hid_input": {
                        "template_path": "/opt/rk3588_gateway/MarkInfo_PelvicFloor_Config_100.json",
                        "non_ascii_focus_clicks": 2,
                        "non_ascii_focus_click_interval_ms": 750,
                    },
                    "vision": {
                        "enabled": True,
                        "wait_after_action": 0.5,
                        "close_msc_popup_when_detected": False,
                    },
                }
            }
            config_path = Path(temp_dir) / "config.yaml"
            write_yaml(config_path, payload)

            config = load_config(config_path)

        self.assertEqual(config.device.type, "盆底肌")
        self.assertEqual(config.hid_input.template_path, "/opt/rk3588_gateway/MarkInfo_PelvicFloor_Config_100.json")
        self.assertEqual(config.hid_input.non_ascii_focus_clicks, 2)
        self.assertEqual(config.hid_input.non_ascii_focus_click_interval_ms, 750)
        self.assertEqual(config.vision.software, "盆底肌")
        self.assertEqual(config.vision.flow, "pelvic_floor")
        self.assertEqual(config.vision.wait_after_action, 0.5)
        self.assertFalse(config.vision.close_msc_popup_when_detected)

    def test_active_profile_can_switch_to_esophageal_motility(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            payload = base_config()
            payload["active_profile"] = "esophageal_motility"
            payload["profiles"] = {
                "esophageal_motility": {
                    "device_type": "\u98df\u9053\u52a8\u529b",
                    "software": "esophageal_motility",
                    "flow": "esophageal_motility",
                    "hid_input": {
                        "template_path": "/opt/rk3588_gateway/MarkInfo_EsophagealMotility_Config_100.json",
                    },
                    "vision": {
                        "enabled": True,
                        "wait_after_action": 0.5,
                        "close_msc_popup_when_detected": False,
                    },
                }
            }
            config_path = Path(temp_dir) / "config.yaml"
            write_yaml(config_path, payload)

            config = load_config(config_path)

        self.assertEqual(config.device.type, "\u98df\u9053\u52a8\u529b")
        self.assertEqual(config.hid_input.template_path, "/opt/rk3588_gateway/MarkInfo_EsophagealMotility_Config_100.json")
        self.assertEqual(config.vision.software, "esophageal_motility")
        self.assertEqual(config.vision.flow, "esophageal_motility")
        self.assertEqual(config.vision.wait_after_action, 0.5)
        self.assertFalse(config.vision.close_msc_popup_when_detected)


if __name__ == "__main__":
    unittest.main()
