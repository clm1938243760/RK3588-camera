from __future__ import annotations

import asyncio
import errno
import logging
import os
import subprocess
import time
from pathlib import Path
from threading import Lock
from typing import Any, Awaitable, Callable, Optional

from .compat import to_thread
from .config import HidInputConfig

LOGGER = logging.getLogger(__name__)
OS_O_NONBLOCK = getattr(os, "O_NONBLOCK", 0)
OS_O_NOCTTY = getattr(os, "O_NOCTTY", 0)
KEY_CAPSLOCK = 0x39
HID_DEVICE_WAIT_SECONDS = 10.0
HID_WRITE_TIMEOUT_SECONDS = 3.0
HID_LED_READ_TIMEOUT_SECONDS = 0.2
HID_USB_WRITE_RETRY_SECONDS = 3.0
HID_USB_WRITE_RETRY_INTERVAL_SECONDS = 0.03
HID_MIN_ACTION_DELAY_SECONDS = 0.15
HID_KEY_HOLD_SECONDS = 0.02
HID_KEY_RELEASE_SECONDS = 0.02
HID_CHAR_DELAY_SECONDS = 0.025
HID_MOUSE_SETTLE_SECONDS = 0.06
HID_MOUSE_HOLD_SECONDS = 0.08
HID_KEYPAD_PLUS = 0x57
HID_KEYPAD_DIGIT = {
    "0": 0x62,
    "1": 0x59,
    "2": 0x5A,
    "3": 0x5B,
    "4": 0x5C,
    "5": 0x5D,
    "6": 0x5E,
    "7": 0x5F,
    "8": 0x60,
    "9": 0x61,
}
CH9350_KEYBOARD_PREFIX = b"\x57\xab\x01"
CH9350_MOUSE_PREFIX = b"\x57\xab\x02"
CH9350_ABS_MOUSE_PREFIX = b"\x57\xab\x04"
CH9350_RELEASE = CH9350_KEYBOARD_PREFIX + bytes(8)

KEY: dict[str, tuple[int, int]] = {
    "\n": (0, 0x28),
    "\t": (0, 0x2B),
    " ": (0, 0x2C),
    "-": (0, 0x2D),
    "=": (0, 0x2E),
    "[": (0, 0x2F),
    "]": (0, 0x30),
    "\\": (0, 0x31),
    ";": (0, 0x33),
    "'": (0, 0x34),
    "`": (0, 0x35),
    ",": (0, 0x36),
    ".": (0, 0x37),
    "/": (0, 0x38),
    "!": (0x02, 0x1E),
    "@": (0x02, 0x1F),
    "#": (0x02, 0x20),
    "$": (0x02, 0x21),
    "%": (0x02, 0x22),
    "^": (0x02, 0x23),
    "&": (0x02, 0x24),
    "*": (0x02, 0x25),
    "(": (0x02, 0x26),
    ")": (0x02, 0x27),
    "_": (0x02, 0x2D),
    "+": (0x02, 0x2E),
    "{": (0x02, 0x2F),
    "}": (0x02, 0x30),
    "|": (0x02, 0x31),
    ":": (0x02, 0x33),
    '"': (0x02, 0x34),
    "~": (0x02, 0x35),
    "<": (0x02, 0x36),
    ">": (0x02, 0x37),
    "?": (0x02, 0x38),
}

NAMED_KEY = {
    "enter": 0x28,
    "escape": 0x29,
    "esc": 0x29,
    "backspace": 0x2A,
    "tab": 0x2B,
    "space": 0x2C,
    "delete": 0x4C,
    "right": 0x4F,
    "left": 0x50,
    "down": 0x51,
    "up": 0x52,
    "home": 0x4A,
    "end": 0x4D,
    "pageup": 0x4B,
    "pagedown": 0x4E,
}
MODIFIER = {
    "ctrl": 0x01,
    "control": 0x01,
    "shift": 0x02,
    "alt": 0x04,
    "win": 0x08,
    "gui": 0x08,
}

for i in range(10):
    KEY[str(i)] = (0, 0x27 if i == 0 else 0x1D + i)
for i, ch in enumerate("abcdefghijklmnopqrstuvwxyz"):
    KEY[ch] = (0, 0x04 + i)
    KEY[ch.upper()] = (0x02, 0x04 + i)


class HidOutput:
    def __init__(self, config: HidInputConfig) -> None:
        self.config = config
        self._led_state: Optional[int] = None
        self._led_task = None
        self._ch9350_fd: Optional[int] = None
        self._ch9350_rx = bytearray()
        self._usb_keyboard_fd: Optional[int] = None
        self._usb_mouse_fd: Optional[int] = None
        self._usb_keyboard_lock = Lock()
        self._usb_mouse_lock = Lock()

    async def execute_form(self, task: dict[str, Any]) -> None:
        if not self.config.enabled:
            LOGGER.info("hid input disabled")
            return
        if self.config.keyboard_backend == "ch9350":
            await self._ensure_ch9350()
        else:
            await self._wait_hid_device(self.config.keyboard_device)
        if self.config.mouse_backend == "usb_gadget":
            await self._wait_hid_device(self.config.mouse_device)
        elif self.config.mouse_backend == "ch9350":
            await self._ensure_ch9350()
        try:
            self._start_led_reader()
            await asyncio.sleep(self.config.start_delay_ms / 1000)

            patient = task.get("patient", {})
            events = task.get("eventClassList", [])
            LOGGER.info("hid form start events=%d patient_id=%s", len(events), patient.get("patient_id", ""))
            for event in events:
                click_type = int(event.get("clickType", -1))
                x = int(event.get("x", 0))
                y = int(event.get("y", 0))
                if click_type == 0:
                    await self.click(x, y)
                elif click_type == 1:
                    await self.input_text(
                        str(event.get("value", "")),
                        x,
                        y,
                        field=str(event.get("field", "")),
                    )
                elif click_type == 7:
                    condition = event.get("condition") or {}
                    if str(patient.get(str(condition.get("field", "")), "")) == str(condition.get("equals", "")):
                        await self.click(x, y)
                else:
                    LOGGER.warning("unknown hid clickType=%s event=%s", click_type, event)
                await asyncio.sleep(max(self.config.action_delay_ms / 1000, HID_MIN_ACTION_DELAY_SECONDS))
            LOGGER.info("hid form done")
        finally:
            self._stop_led_reader()
            if self.config.keyboard_backend == "usb_gadget" or self.config.mouse_backend == "usb_gadget":
                await to_thread(self.close_usb_gadget_fds, "after hid form")

    async def execute_actions(
        self,
        actions: list[dict[str, Any]],
        patient: dict[str, Any],
        wait_for_text: Optional[Callable[[str, float], Awaitable[bool]]] = None,
    ) -> None:
        """Execute the report-center action schema with the verified HID backends."""
        if not self.config.enabled:
            raise RuntimeError("hid input is disabled")
        if self.config.keyboard_backend == "ch9350":
            await self._ensure_ch9350()
        else:
            await self._wait_hid_device(self.config.keyboard_device)
        if self.config.mouse_backend == "usb_gadget":
            await self._wait_hid_device(self.config.mouse_device)
        elif self.config.mouse_backend == "ch9350":
            await self._ensure_ch9350()
        try:
            self._start_led_reader()
            await asyncio.sleep(self.config.start_delay_ms / 1000)
            for action in actions:
                await self._execute_configured_action(action, patient, wait_for_text)
                delay_ms = int(action.get("delay_after_ms", self.config.action_delay_ms))
                await asyncio.sleep(max(0, delay_ms) / 1000)
        finally:
            self._stop_led_reader()
            if self.config.keyboard_backend == "usb_gadget" or self.config.mouse_backend == "usb_gadget":
                await to_thread(self.close_usb_gadget_fds, "after configured hid flow")

    async def _execute_configured_action(
        self,
        action: dict[str, Any],
        patient: dict[str, Any],
        wait_for_text: Optional[Callable[[str, float], Awaitable[bool]]],
    ) -> None:
        action_type = str(action.get("type", "")).strip()
        x, y = int(action.get("x", 0)), int(action.get("y", 0))
        if action_type == "click":
            await self.click(x, y)
        elif action_type == "double_click":
            await self.click(x, y)
            await asyncio.sleep(max(20, int(action.get("interval_ms", 120))) / 1000)
            await self.click(x, y)
        elif action_type in {"input_field", "input_literal"}:
            if action_type == "input_field":
                field = str(action.get("field", ""))
                value = _patient_field(patient, field)
            else:
                field = ""
                value = str(action.get("value", ""))
            await self.input_text(value, x, y, field=field)
        elif action_type in {"key", "hotkey"}:
            modifiers = action.get("modifiers", []) if action_type == "hotkey" else []
            await self._press_named_key(str(action.get("key", "")), modifiers)
        elif action_type == "wait":
            milliseconds = int(action.get("milliseconds", action.get("duration_ms", 0)))
            await asyncio.sleep(max(0, milliseconds) / 1000)
        elif action_type == "condition":
            field = str(action.get("field", ""))
            if _patient_field(patient, field) == str(action.get("equals", "")):
                nested = action.get("action")
                if isinstance(nested, dict):
                    await self._execute_configured_action(nested, patient, wait_for_text)
                elif "x" in action and "y" in action:
                    await self.click(x, y)
        elif action_type == "wait_for_text":
            if wait_for_text is None:
                raise RuntimeError("wait_for_text requires the KVM OCR provider")
            text = str(action.get("text", "")).strip()
            timeout = max(0.1, float(action.get("timeout_seconds", 10)))
            if not text or not await wait_for_text(text, timeout):
                raise RuntimeError("wait_for_text timed out")
        else:
            raise ValueError("unsupported HID action: %s" % action_type)

    async def _press_named_key(self, key: str, modifiers: Any) -> None:
        normalized = key.strip().lower()
        if len(normalized) == 1 and normalized in KEY:
            default_mod, code = KEY[normalized]
        elif normalized in NAMED_KEY:
            default_mod, code = 0, NAMED_KEY[normalized]
        else:
            raise ValueError("unsupported HID key: %s" % key)
        modifier = default_mod
        if isinstance(modifiers, list):
            for item in modifiers:
                name = str(item).strip().lower()
                if name not in MODIFIER:
                    raise ValueError("unsupported HID modifier: %s" % item)
                modifier |= MODIFIER[name]
        await self._press_key(modifier, code)

    async def click(self, x: int, y: int) -> None:
        if self.config.mouse_backend == "ch9350":
            await self.ch9350_click_abs(x, y)
            return
        if self.config.mouse_backend != "usb_gadget":
            LOGGER.warning("mouse backend %s does not support click", self.config.mouse_backend)
            return
        ax = max(0, min(32767, int(x * 32767 / max(self.config.screen_width - 1, 1))))
        ay = max(0, min(32767, int(y * 32767 / max(self.config.screen_height - 1, 1))))
        LOGGER.info("hid mouse click x=%d y=%d", x, y)
        await self._write_mouse(0, ax, ay)
        await asyncio.sleep(HID_MOUSE_SETTLE_SECONDS)
        try:
            await self._write_mouse(1, ax, ay)
            await asyncio.sleep(HID_MOUSE_HOLD_SECONDS)
        finally:
            await asyncio.shield(self._write_mouse(0, ax, ay))

    async def ch9350_click_abs(self, x: int, y: int) -> None:
        LOGGER.info("ch9350 mouse click target x=%d y=%d", x, y)
        if self.config.ch9350_mouse_frame == "absolute7":
            await self._write_ch9350_abs_mouse(button=0, x=x, y=y, wheel=0)
            await asyncio.sleep(HID_MOUSE_SETTLE_SECONDS)
            try:
                await self._write_ch9350_abs_mouse(button=1, x=x, y=y, wheel=0)
                await asyncio.sleep(HID_MOUSE_HOLD_SECONDS)
            finally:
                await asyncio.shield(self._write_ch9350_abs_mouse(button=0, x=x, y=y, wheel=0))
            return

        if self.config.ch9350_mouse_reset_to_origin:
            await self.ch9350_move_relative(-127, -127, repeat=24)
            await asyncio.sleep(HID_MOUSE_HOLD_SECONDS)
        await self.ch9350_move_to(x, y)
        await asyncio.sleep(HID_MOUSE_SETTLE_SECONDS)
        try:
            await self._write_ch9350_mouse(button=1, dx=0, dy=0, wheel=0)
            await asyncio.sleep(HID_MOUSE_HOLD_SECONDS)
        finally:
            await asyncio.shield(self._write_ch9350_mouse(button=0, dx=0, dy=0, wheel=0))

    async def ch9350_move_to(self, x: int, y: int) -> None:
        target_x = max(0, min(self.config.screen_width - 1, x))
        target_y = max(0, min(self.config.screen_height - 1, y))
        if not self.config.ch9350_mouse_reset_to_origin:
            LOGGER.warning("ch9350 absolute target requested without origin reset; using relative dx/dy directly")
        await self.ch9350_move_relative(target_x, target_y)

    async def ch9350_move_relative(self, dx: int, dy: int, repeat: Optional[int] = None) -> None:
        if repeat is not None:
            for _ in range(repeat):
                await self._write_ch9350_mouse(button=0, dx=dx, dy=dy, wheel=0)
                await asyncio.sleep(0.01)
            return

        remaining_x = dx
        remaining_y = dy
        while remaining_x or remaining_y:
            step_x = max(-127, min(127, remaining_x))
            step_y = max(-127, min(127, remaining_y))
            await self._write_ch9350_mouse(button=0, dx=step_x, dy=step_y, wheel=0)
            remaining_x -= step_x
            remaining_y -= step_y
            await asyncio.sleep(0.01)

    async def input_text(self, text: str, x: int, y: int, field: str = "") -> None:
        if not text:
            return
        if all(ch in KEY for ch in text):
            LOGGER.info("hid input field=%s ascii text=%s", field, text)
            await self.click(x, y)
            await asyncio.sleep(0.025)
            await self.select_all()
            await self.type_ascii(text)
        elif self.config.non_ascii_mode == "powershell":
            LOGGER.info("hid input field=%s non-ascii text=%s", field, text)
            await self.paste_text_windows(text, x, y)
        elif self.config.non_ascii_mode == "alt_numpad_hex":
            LOGGER.info("hid input field=%s unicode alt-numpad chars=%d", field, len(text))
            await self.click(x, y)
            await asyncio.sleep(0.025)
            await self.select_all()
            await self.type_unicode_alt_numpad_hex(text)
        else:
            LOGGER.warning("skip non-ascii hid text len=%d text=%s", len(text), text)

    async def type_ascii(self, text: str) -> None:
        LOGGER.info("hid type ascii len=%d text=%s", len(text), text)
        if self.config.force_caps_ascii:
            await self.type_ascii_caps_guard(text)
            return
        for ch in text:
            mod, code = KEY[ch]
            await self._press_key(mod, code)
            await asyncio.sleep(HID_CHAR_DELAY_SECONDS)

    async def type_ascii_caps_guard(self, text: str) -> None:
        old_caps = await self._wait_caps()
        await self._ensure_caps(True)
        try:
            for ch in text.lower():
                if ch not in KEY:
                    LOGGER.warning("unsupported ascii char skipped: %r", ch)
                    continue
                mod, code = KEY[ch]
                await self._press_key(mod, code)
                await asyncio.sleep(HID_CHAR_DELAY_SECONDS)
        finally:
            await self._ensure_caps(bool(old_caps) if old_caps is not None else False)

    async def paste_text_windows(self, text: str, x: int, y: int) -> None:
        command = self._powershell_clipboard_command(text)
        LOGGER.info("hid paste text len=%d", len(text))
        await self._press_key(0x08, 0x15)  # Win+R
        await asyncio.sleep(0.35)
        await self.select_all()
        await self.type_ascii_caps_guard(command)
        await self._press_key(0, 0x28)  # Enter
        await asyncio.sleep(self.config.powershell_wait_ms / 1000)
        focus_clicks = max(1, int(getattr(self.config, "non_ascii_focus_clicks", 1)))
        interval = max(0, int(getattr(self.config, "non_ascii_focus_click_interval_ms", 100))) / 1000
        for _ in range(focus_clicks):
            await self.click(x, y)
            await asyncio.sleep(interval)
        await self.select_all()
        await self._press_key(0x01, 0x19)  # Ctrl+V

    def _powershell_clipboard_command(self, text: str) -> str:
        parts = "+".join(f"[char]{ord(ch)}" for ch in text)
        return f"powershell -sta -nop -w hidden -c \"Set-Clipboard -Value ({parts})\""

    async def type_unicode_alt_numpad_hex(self, text: str) -> None:
        """Enter Unicode code points through Windows EnableHexNumpad input."""
        LOGGER.info("hid type unicode alt-numpad chars=%d", len(text))
        for character in text:
            digits = format(ord(character), "x")
            try:
                await self._press_key_with_held_modifier(0x04, HID_KEYPAD_PLUS)
                for digit in digits:
                    code = HID_KEYPAD_DIGIT.get(digit)
                    if code is None:
                        code = KEY[digit][1]
                    await self._press_key_with_held_modifier(0x04, code)
            finally:
                await asyncio.shield(self._write_keyboard(bytes(8)))
            await asyncio.sleep(HID_CHAR_DELAY_SECONDS)

    async def select_all(self) -> None:
        await self._press_key(0x01, 0x04)  # Ctrl+A
        await asyncio.sleep(0.05)

    async def _press_key(self, mod: int, code: int) -> None:
        await self._write_keyboard(bytes([mod, 0, code, 0, 0, 0, 0, 0]))
        try:
            await asyncio.sleep(HID_KEY_HOLD_SECONDS)
        finally:
            await asyncio.shield(self._write_keyboard(bytes(8)))
        await asyncio.sleep(HID_KEY_RELEASE_SECONDS)

    async def _press_key_with_held_modifier(self, mod: int, code: int) -> None:
        await self._write_keyboard(bytes([mod, 0, code, 0, 0, 0, 0, 0]))
        try:
            await asyncio.sleep(HID_KEY_HOLD_SECONDS)
        finally:
            await asyncio.shield(
                self._write_keyboard(bytes([mod, 0, 0, 0, 0, 0, 0, 0]))
            )
        await asyncio.sleep(HID_KEY_RELEASE_SECONDS)

    def _start_led_reader(self) -> None:
        if self.config.keyboard_backend != "ch9350":
            # RK3568 vendor 4.19 can Oops in f_hidg_read if /dev/hidg0 is
            # read continuously. USB gadget LEDs are sampled on demand instead.
            return
        if self._led_task and not self._led_task.done():
            return
        self._led_task = asyncio.create_task(self._led_reader_loop())

    def _stop_led_reader(self) -> None:
        if self._led_task and not self._led_task.done():
            self._led_task.cancel()

    async def _led_reader_loop(self) -> None:
        if self.config.keyboard_backend == "ch9350":
            await self._ch9350_led_reader_loop()
            return

    async def _ch9350_led_reader_loop(self) -> None:
        while True:
            try:
                await self._ensure_ch9350()
                assert self._ch9350_fd is not None
                LOGGER.info("ch9350 serial reader start: %s", self.config.ch9350_serial_device)
                while True:
                    try:
                        data = os.read(self._ch9350_fd, 64)
                        if data:
                            self._parse_ch9350_rx(data)
                    except BlockingIOError:
                        await asyncio.sleep(0.02)
            except asyncio.CancelledError:
                raise
            except Exception:
                LOGGER.exception("ch9350 serial reader failed")
                await asyncio.sleep(1)

    def _parse_ch9350_rx(self, data: bytes) -> None:
        self._ch9350_rx.extend(data)
        while len(self._ch9350_rx) >= 4:
            if self._ch9350_rx[0] != 0x57 or self._ch9350_rx[1] != 0xAB:
                del self._ch9350_rx[0]
                continue
            frame_type = self._ch9350_rx[2]
            if frame_type == 0x80:
                status = self._ch9350_rx[3]
                self._led_state = status
                LOGGER.info(
                    "ch9350 status frame raw=0x%02x caps=%s",
                    status,
                    "on" if status & self.config.ch9350_caps_led_mask else "off",
                )
                del self._ch9350_rx[:4]
                continue
            if frame_type == 0x01 and len(self._ch9350_rx) >= 11:
                LOGGER.debug("ch9350 keyboard rx frame=%s", self._ch9350_rx[:11].hex(" "))
                del self._ch9350_rx[:11]
                continue
            if frame_type == 0x02 and len(self._ch9350_rx) >= 7:
                LOGGER.debug("ch9350 mouse rx frame=%s", self._ch9350_rx[:7].hex(" "))
                del self._ch9350_rx[:7]
                continue
            break

    def _get_caps(self) -> Optional[bool]:
        if self._led_state is None:
            return None
        if self.config.keyboard_backend == "ch9350":
            return bool(self._led_state & self.config.ch9350_caps_led_mask)
        return bool(self._led_state & 2)

    async def _wait_caps(self, timeout: float = 0.5) -> Optional[bool]:
        end = asyncio.get_running_loop().time() + timeout
        state = self._get_caps()
        if state is not None:
            return state
        while asyncio.get_running_loop().time() < end:
            if self.config.keyboard_backend != "ch9350":
                await self._refresh_usb_keyboard_led()
            state = self._get_caps()
            if state is not None:
                return state
            await asyncio.sleep(0.05)
        return self._get_caps()

    async def _ensure_caps(self, target: bool) -> None:
        state = await self._wait_caps()
        if state is target:
            return
        if self.config.keyboard_backend != "ch9350":
            self._led_state = None
        await self._press_key(0, KEY_CAPSLOCK)
        await asyncio.sleep(0.2)
        state = await self._wait_caps()
        if state is not None and state is not target:
            if self.config.keyboard_backend != "ch9350":
                self._led_state = None
            await self._press_key(0, KEY_CAPSLOCK)
            await asyncio.sleep(0.2)

    async def _write_keyboard(self, report: bytes) -> None:
        if self.config.keyboard_backend == "ch9350":
            await self._write_ch9350_keyboard(report)
            return
        await self._write_usb_keyboard(report)

    async def _write_mouse(self, button: int, ax: int, ay: int) -> None:
        report = bytes([button, ax & 0xFF, (ax >> 8) & 0xFF, ay & 0xFF, (ay >> 8) & 0xFF])
        await asyncio.wait_for(
            to_thread(self._write_usb_mouse_once, report),
            timeout=HID_WRITE_TIMEOUT_SECONDS,
        )

    async def _ensure_usb_keyboard_fd(self) -> None:
        if self._usb_keyboard_fd is not None:
            return
        await self._wait_hid_device(self.config.keyboard_device)
        await to_thread(self._open_usb_keyboard_fd)

    def _open_usb_keyboard_fd(self) -> None:
        with self._usb_keyboard_lock:
            if self._usb_keyboard_fd is None:
                self._usb_keyboard_fd = os.open(self.config.keyboard_device, os.O_RDWR | OS_O_NONBLOCK)
                LOGGER.info("usb keyboard gadget opened read-write: %s", self.config.keyboard_device)

    def _close_usb_keyboard_fd(self, timeout_seconds: float = 2.0) -> bool:
        if not self._usb_keyboard_lock.acquire(timeout=timeout_seconds):
            LOGGER.warning("usb keyboard fd close timeout because hid write lock is busy")
            return False
        try:
            fd = self._usb_keyboard_fd
            self._usb_keyboard_fd = None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return True
        finally:
            self._usb_keyboard_lock.release()

    def _close_usb_mouse_fd(self, timeout_seconds: float = 2.0) -> bool:
        if not self._usb_mouse_lock.acquire(timeout=timeout_seconds):
            LOGGER.warning("usb mouse fd close timeout because hid write lock is busy")
            return False
        try:
            fd = self._usb_mouse_fd
            self._usb_mouse_fd = None
            if fd is not None:
                try:
                    os.close(fd)
                except OSError:
                    pass
            return True
        finally:
            self._usb_mouse_lock.release()

    def close_usb_gadget_fds(self, reason: str = "") -> bool:
        keyboard_open = self._usb_keyboard_fd is not None
        mouse_open = self._usb_mouse_fd is not None
        keyboard_closed = self._close_usb_keyboard_fd()
        mouse_closed = self._close_usb_mouse_fd()
        self._led_state = None
        if keyboard_open or mouse_open:
            LOGGER.info(
                "hid usb gadget fds close result reason=%s keyboard_open=%s mouse_open=%s keyboard_closed=%s mouse_closed=%s",
                reason or "unspecified",
                keyboard_open,
                mouse_open,
                keyboard_closed,
                mouse_closed,
            )
        return keyboard_closed and mouse_closed

    async def _write_usb_keyboard(self, report: bytes) -> None:
        await self._ensure_usb_keyboard_fd()
        try:
            await asyncio.wait_for(
                to_thread(self._write_usb_keyboard_once, report),
                timeout=HID_WRITE_TIMEOUT_SECONDS,
            )
        except OSError:
            LOGGER.exception("usb keyboard write failed")
            raise

    def _write_usb_keyboard_once(self, report: bytes) -> None:
        with self._usb_keyboard_lock:
            if self._usb_keyboard_fd is None:
                raise FileNotFoundError(self.config.keyboard_device)
            os.write(self._usb_keyboard_fd, report)

    def _write_usb_mouse_once(self, report: bytes) -> None:
        with self._usb_mouse_lock:
            if self._usb_mouse_fd is None:
                self._usb_mouse_fd = os.open(self.config.mouse_device, os.O_WRONLY | OS_O_NONBLOCK)
                LOGGER.info("usb mouse gadget opened write-only: %s", self.config.mouse_device)
            deadline = time.monotonic() + HID_USB_WRITE_RETRY_SECONDS
            while True:
                try:
                    os.write(self._usb_mouse_fd, report)
                    return
                except BlockingIOError as exc:
                    if exc.errno != errno.EAGAIN or time.monotonic() >= deadline:
                        raise
                    time.sleep(HID_USB_WRITE_RETRY_INTERVAL_SECONDS)

    async def _refresh_usb_keyboard_led(self) -> None:
        await self._ensure_usb_keyboard_fd()
        try:
            data = await asyncio.wait_for(
                to_thread(self._read_usb_keyboard_led_once),
                timeout=HID_LED_READ_TIMEOUT_SECONDS,
            )
        except (asyncio.TimeoutError, OSError):
            return
        if not data:
            return
        self._led_state = data[0]
        LOGGER.info(
            "keyboard led state=0x%02x caps=%s",
            data[0],
            "on" if data[0] & 2 else "off",
        )

    def _read_usb_keyboard_led_once(self) -> bytes:
        with self._usb_keyboard_lock:
            if self._usb_keyboard_fd is None:
                raise FileNotFoundError(self.config.keyboard_device)
            latest = b""
            try:
                while True:
                    latest = os.read(self._usb_keyboard_fd, 8) or latest
            except BlockingIOError:
                return latest

    async def _ensure_ch9350(self) -> None:
        if self._ch9350_fd is not None:
            return
        await self._wait_device(self.config.ch9350_serial_device)
        await to_thread(self._configure_ch9350_serial)
        fd = os.open(self.config.ch9350_serial_device, os.O_RDWR | OS_O_NOCTTY | OS_O_NONBLOCK)
        self._ch9350_fd = fd
        LOGGER.info("ch9350 serial opened: %s baud=%d", self.config.ch9350_serial_device, self.config.ch9350_baudrate)
        if self.config.ch9350_set_state2 or self.config.ch9350_state:
            state = 2 if self.config.ch9350_set_state2 else self.config.ch9350_state
            os.write(fd, bytes([0x57, 0xAB, 0x40, state & 0xFF]))
            LOGGER.info("ch9350 set state%d frame sent", state)
            await asyncio.sleep(0.2)

    async def _wait_hid_device(self, path: str) -> None:
        await asyncio.wait_for(self._wait_device(path), timeout=HID_DEVICE_WAIT_SECONDS)

    def _configure_ch9350_serial(self) -> None:
        subprocess.run(
            [
                "stty",
                "-F",
                self.config.ch9350_serial_device,
                str(self.config.ch9350_baudrate),
                "cs8",
                "-cstopb",
                "-parenb",
                "raw",
                "-echo",
            ],
            check=True,
        )

    async def _write_ch9350_keyboard(self, report: bytes) -> None:
        await self._ensure_ch9350()
        assert self._ch9350_fd is not None
        frame = CH9350_KEYBOARD_PREFIX + report
        await to_thread(os.write, self._ch9350_fd, frame)

    async def _write_ch9350_mouse(self, button: int, dx: int, dy: int, wheel: int = 0) -> None:
        await self._ensure_ch9350()
        assert self._ch9350_fd is not None
        if self.config.ch9350_mouse_frame != "relative4":
            LOGGER.warning("unsupported ch9350 mouse frame mode=%s, using relative4", self.config.ch9350_mouse_frame)
        report = bytes([button & 0x07, dx & 0xFF, dy & 0xFF, wheel & 0xFF])
        frame = CH9350_MOUSE_PREFIX + report
        await to_thread(os.write, self._ch9350_fd, frame)

    async def _write_ch9350_abs_mouse(self, button: int, x: int, y: int, wheel: int = 0) -> None:
        await self._ensure_ch9350()
        assert self._ch9350_fd is not None
        ax = max(0, min(0x3FF, int(x * 0x3FF / max(self.config.screen_width - 1, 1))))
        ay = max(0, min(0x3FF, int(y * 0x3FF / max(self.config.screen_height - 1, 1))))
        report = bytes([
            0x01,
            button & 0x07,
            ax & 0xFF,
            (ax >> 8) & 0xFF,
            ay & 0xFF,
            (ay >> 8) & 0xFF,
            wheel & 0xFF,
        ])
        frame = CH9350_ABS_MOUSE_PREFIX + report
        LOGGER.info("ch9350 abs mouse report x=%d y=%d ax=%d ay=%d button=%d", x, y, ax, ay, button)
        await to_thread(os.write, self._ch9350_fd, frame)

    async def _wait_device(self, path: str) -> None:
        for _ in range(60):
            if Path(path).exists():
                return
            LOGGER.warning("waiting for hid device: %s", path)
            await asyncio.sleep(1)
        raise FileNotFoundError(path)


def _patient_field(patient: dict[str, Any], field: str) -> str:
    if field.startswith("extra_fields."):
        extra = patient.get("extra_fields", {})
        return str(extra.get(field.split(".", 1)[1], "") if isinstance(extra, dict) else "")
    return str(patient.get(field, "") or "")
