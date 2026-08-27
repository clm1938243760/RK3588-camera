import tempfile
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rk3588_gateway.msc_monitor import MscMonitor


class FakeAttr:
    def __init__(self, value=""):
        self.value = value
        self.writes = []

    def exists(self):
        return True

    def read_text(self, encoding="utf-8"):
        return self.value

    def write_text(self, value, encoding="utf-8"):
        self.writes.append(value)
        self.value = value
        return len(value)


def make_monitor(tmpdir):
    config = SimpleNamespace(
        image_path=str(Path(tmpdir) / "ums.img"),
        mount_dir=str(Path(tmpdir) / "mnt"),
        output_dir=str(Path(tmpdir) / "out"),
        state_dir=str(Path(tmpdir) / "state"),
        gadget_dir=str(Path(tmpdir) / "gadget"),
        udc_device="fc400000.usb",
    )
    return MscMonitor(config, queue=None, device_id="dev1")


class MscMonitorBindTest(unittest.TestCase):
    def test_bind_is_noop_when_gadget_is_already_bound(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = make_monitor(tmpdir)
            attr = FakeAttr("fc400000.usb")
            monitor._udc_attr = lambda: attr

            monitor._bind_gadget("fc400000.usb")

            self.assertEqual(attr.writes, [])

    def test_busy_bind_logs_current_owner(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            monitor = make_monitor(tmpdir)

            class BusyAttr(FakeAttr):
                def write_text(self, value, encoding="utf-8"):
                    raise OSError(16, "Device or resource busy")

            monitor._udc_attr = lambda: BusyAttr("")
            monitor._find_udc_owner = lambda udc: "/sys/kernel/config/usb_gadget/test_c1_msc"

            with patch("rk3588_gateway.msc_monitor.LOGGER") as logger:
                monitor._bind_gadget("fc400000.usb")

            self.assertIn("owner=%s", logger.exception.call_args.args[0])
            self.assertEqual(logger.exception.call_args.args[2], "/sys/kernel/config/usb_gadget/test_c1_msc")


if __name__ == "__main__":
    unittest.main()
