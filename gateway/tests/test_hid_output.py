import errno
import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

import rk3588_gateway.hid_output as hid_module
from rk3588_gateway.hid_output import HidOutput


class HidOutputTest(unittest.TestCase):
    def test_alt_numpad_hex_emits_keypad_plus_hex_digits_and_releases_alt(self):
        output = HidOutput(SimpleNamespace(non_ascii_mode="alt_numpad_hex"))
        writes = []

        async def fake_write(report):
            writes.append(report)

        output._write_keyboard = fake_write
        old_hold = hid_module.HID_KEY_HOLD_SECONDS
        old_release = hid_module.HID_KEY_RELEASE_SECONDS
        old_char = hid_module.HID_CHAR_DELAY_SECONDS
        try:
            hid_module.HID_KEY_HOLD_SECONDS = 0
            hid_module.HID_KEY_RELEASE_SECONDS = 0
            hid_module.HID_CHAR_DELAY_SECONDS = 0
            asyncio.run(output.type_unicode_alt_numpad_hex("安"))
        finally:
            hid_module.HID_KEY_HOLD_SECONDS = old_hold
            hid_module.HID_KEY_RELEASE_SECONDS = old_release
            hid_module.HID_CHAR_DELAY_SECONDS = old_char

        pressed = [report[2] for report in writes if report[0] == 0x04 and report[2]]
        self.assertEqual(pressed, [0x57, 0x5D, 0x05, 0x60, 0x61])
        self.assertEqual(writes[-1], bytes(8))

    def test_non_ascii_alt_numpad_clicks_and_selects_target(self):
        output = HidOutput(SimpleNamespace(non_ascii_mode="alt_numpad_hex"))
        calls = []

        async def fake_click(x, y):
            calls.append(("click", x, y))

        async def fake_select_all():
            calls.append(("select_all",))

        async def fake_type_unicode(text):
            calls.append(("unicode", text))

        output.click = fake_click
        output.select_all = fake_select_all
        output.type_unicode_alt_numpad_hex = fake_type_unicode

        asyncio.run(output.input_text("周安楠", 682, 391, field="patient_name"))

        self.assertEqual(calls, [
            ("click", 682, 391),
            ("select_all",),
            ("unicode", "周安楠"),
        ])

    def test_non_ascii_paste_can_focus_with_two_clicks(self):
        output = HidOutput(
            SimpleNamespace(
                non_ascii_mode="powershell",
                powershell_wait_ms=0,
                non_ascii_focus_clicks=2,
                non_ascii_focus_click_interval_ms=750,
            )
        )
        clicks = []
        keys = []

        async def fake_click(x, y):
            clicks.append((x, y))

        async def fake_press_key(mod, code):
            keys.append((mod, code))

        async def fake_type_ascii_caps_guard(text):
            self.assertIn("powershell", text)

        async def fake_select_all():
            keys.append(("select_all", None))

        output.click = fake_click
        output._press_key = fake_press_key
        output.type_ascii_caps_guard = fake_type_ascii_caps_guard
        output.select_all = fake_select_all

        asyncio.run(output.input_text("测试", 304, 257, field="patient_name"))

        self.assertEqual(clicks, [(304, 257), (304, 257)])
        self.assertIn((0x01, 0x19), keys)

    def test_usb_mouse_write_retries_eagain(self):
        output = HidOutput(SimpleNamespace(mouse_device="/dev/hidg1"))
        original_open = hid_module.os.open
        original_write = hid_module.os.write
        original_sleep = hid_module.time.sleep
        original_close = hid_module.os.close
        writes = []

        def fake_open(path, flags):
            self.assertEqual(path, "/dev/hidg1")
            return 99

        def fake_write(fd, report):
            self.assertEqual(fd, 99)
            writes.append(report)
            if len(writes) < 3:
                raise BlockingIOError(errno.EAGAIN, "try again")
            return len(report)

        try:
            hid_module.os.open = fake_open
            hid_module.os.write = fake_write
            hid_module.os.close = lambda fd: None
            hid_module.time.sleep = lambda seconds: None
            output._write_usb_mouse_once(b"mouse")
        finally:
            hid_module.os.open = original_open
            hid_module.os.write = original_write
            hid_module.os.close = original_close
            hid_module.time.sleep = original_sleep
            output.close_usb_gadget_fds("test cleanup")

        self.assertEqual(writes, [b"mouse", b"mouse", b"mouse"])


if __name__ == "__main__":
    unittest.main()
