from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable

from evdev import InputDevice, categorize, ecodes

from .config import ScannerConfig
from .events import GatewayEvent

LOGGER = logging.getLogger(__name__)

KEY_MAP = {
    "KEY_0": "0",
    "KEY_1": "1",
    "KEY_2": "2",
    "KEY_3": "3",
    "KEY_4": "4",
    "KEY_5": "5",
    "KEY_6": "6",
    "KEY_7": "7",
    "KEY_8": "8",
    "KEY_9": "9",
    "KEY_A": "a",
    "KEY_B": "b",
    "KEY_C": "c",
    "KEY_D": "d",
    "KEY_E": "e",
    "KEY_F": "f",
    "KEY_G": "g",
    "KEY_H": "h",
    "KEY_I": "i",
    "KEY_J": "j",
    "KEY_K": "k",
    "KEY_L": "l",
    "KEY_M": "m",
    "KEY_N": "n",
    "KEY_O": "o",
    "KEY_P": "p",
    "KEY_Q": "q",
    "KEY_R": "r",
    "KEY_S": "s",
    "KEY_T": "t",
    "KEY_U": "u",
    "KEY_V": "v",
    "KEY_W": "w",
    "KEY_X": "x",
    "KEY_Y": "y",
    "KEY_Z": "z",
    "KEY_MINUS": "-",
    "KEY_EQUAL": "=",
    "KEY_DOT": ".",
    "KEY_SLASH": "/",
    "KEY_SPACE": " ",
}

SHIFT_KEY_MAP = {
    "KEY_1": "!",
    "KEY_2": "@",
    "KEY_3": "#",
    "KEY_4": "$",
    "KEY_5": "%",
    "KEY_6": "^",
    "KEY_7": "&",
    "KEY_8": "*",
    "KEY_9": "(",
    "KEY_0": ")",
    "KEY_MINUS": "_",
    "KEY_EQUAL": "+",
    "KEY_DOT": ">",
    "KEY_SLASH": "?",
}


class ScannerReader:
    def __init__(self, config: ScannerConfig, device_id: str) -> None:
        self.config = config
        self.device_id = device_id
        self._stop = asyncio.Event()

    def stop(self) -> None:
        self._stop.set()

    async def run(self, on_event: Callable[[GatewayEvent], Awaitable[None]]) -> None:
        if not self.config.enabled:
            LOGGER.info("scanner disabled")
            return
        if not self.config.event_device:
            LOGGER.warning("scanner event_device is empty")
            return

        while not self._stop.is_set():
            try:
                await self._read_loop(on_event)
            except FileNotFoundError:
                LOGGER.warning("scanner device not found: %s", self.config.event_device)
            except PermissionError:
                LOGGER.error("permission denied reading scanner: %s", self.config.event_device)
            except Exception:
                LOGGER.exception("scanner loop failed")
            await asyncio.sleep(3)

    async def _read_loop(self, on_event: Callable[[GatewayEvent], Awaitable[None]]) -> None:
        device = InputDevice(self.config.event_device)
        LOGGER.info("scanner opened: %s (%s)", device.name, self.config.event_device)
        buffer: list[str] = []
        shift = False

        async for raw_event in device.async_read_loop():
            if self._stop.is_set():
                break
            if raw_event.type != ecodes.EV_KEY:
                continue
            event = categorize(raw_event)
            keycode = event.keycode
            if isinstance(keycode, list):
                keycode = keycode[0]

            if keycode in ("KEY_LEFTSHIFT", "KEY_RIGHTSHIFT"):
                shift = event.keystate == event.key_down
                continue
            if event.keystate != event.key_down:
                continue

            if keycode in self.config.terminator_keys:
                code = "".join(buffer).strip()
                buffer.clear()
                if len(code) >= self.config.min_length:
                    await on_event(
                        GatewayEvent(
                            type="barcode.scan",
                            device_id=self.device_id,
                            payload={"code": code, "source": self.config.event_device},
                        )
                    )
                elif code:
                    LOGGER.warning("ignore short scanner code=%s length=%d", code, len(code))
                continue

            char = SHIFT_KEY_MAP.get(keycode) if shift else KEY_MAP.get(keycode)
            if char:
                buffer.append(char.upper() if shift and len(char) == 1 and char.isalpha() else char)
