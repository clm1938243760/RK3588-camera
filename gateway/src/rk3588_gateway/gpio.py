from __future__ import annotations

import asyncio
import logging
import shutil
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .compat import to_thread
from .config import GpioConfig, GpioLineConfig

LOGGER = logging.getLogger(__name__)


@dataclass
class GpioState:
    config: GpioLineConfig
    value: int
    process: Optional[subprocess.Popen] = None


class GpioController:
    def __init__(self, config: GpioConfig) -> None:
        self.config = config
        self._lines: dict[str, GpioState] = {}
        self._gpioset = shutil.which("gpioset")
        self._gpioget = shutil.which("gpioget")

    async def start(self) -> None:
        if not self.config.enabled:
            LOGGER.info("gpio disabled")
            return

        for line in self.config.lines:
            if not line.enabled:
                continue
            if line.backend not in {"gpiod", "sysfs"}:
                LOGGER.warning("gpio %s invalid backend=%s", line.name, line.backend)
                continue
            if line.backend == "gpiod" and (not self._gpioset or not self._gpioget):
                LOGGER.warning("gpio tools missing, install package: gpiod")
                continue
            if line.direction not in {"in", "out"}:
                LOGGER.warning("gpio %s invalid direction=%s", line.name, line.direction)
                continue
            state = GpioState(config=line, value=self._normalize(line.default))
            self._lines[line.name] = state
            if line.backend == "sysfs":
                await to_thread(self._prepare_sysfs, line)
            if line.direction == "out":
                await self.set_value(line.name, state.value)
            else:
                state.value = await self.read_value(line.name)
            LOGGER.info(
                "gpio ready name=%s backend=%s chip=%s line=%d number=%d dir=%s",
                line.name,
                line.backend,
                line.chip,
                line.line,
                line.number,
                line.direction,
            )

    async def stop(self) -> None:
        for state in self._lines.values():
            self._stop_process(state)

    def snapshot(self) -> list[dict[str, object]]:
        return [
            {
                "name": state.config.name,
                "enabled": state.config.enabled,
                "backend": state.config.backend,
                "chip": state.config.chip,
                "line": state.config.line,
                "number": state.config.number,
                "direction": state.config.direction,
                "active_low": state.config.active_low,
                "value": state.value,
            }
            for state in self._lines.values()
        ]

    async def refresh_inputs(self) -> None:
        for name, state in self._lines.items():
            if state.config.direction == "in":
                await self.read_value(name)

    async def read_value(self, name: str) -> int:
        state = self._require_line(name)
        if state.config.direction == "out":
            return state.value
        if state.config.backend == "sysfs":
            raw = await to_thread(self._read_sysfs, state.config)
        else:
            raw = await to_thread(self._run_gpioget, state.config)
        state.value = raw
        return state.value

    async def set_value(self, name: str, value: int) -> int:
        state = self._require_line(name)
        if state.config.direction != "out":
            raise ValueError(f"gpio {name} is not output")
        state.value = self._normalize(value)
        if state.config.backend == "sysfs":
            await to_thread(self._write_sysfs, state.config, state.value)
        else:
            await to_thread(self._start_gpioset, state)
        return state.value

    async def pulse(self, name: str, value: int = 1, duration_ms: int = 200) -> int:
        state = self._require_line(name)
        if state.config.direction != "out":
            raise ValueError(f"gpio {name} is not output")
        active = self._normalize(value)
        inactive = 0 if active else 1
        await self.set_value(name, active)
        await asyncio.sleep(max(duration_ms, 1) / 1000)
        await self.set_value(name, inactive)
        return inactive

    def _require_line(self, name: str) -> GpioState:
        try:
            return self._lines[name]
        except KeyError as exc:
            raise KeyError(f"unknown gpio: {name}") from exc

    def _start_gpioset(self, state: GpioState) -> None:
        if not self._gpioset:
            raise RuntimeError("gpioset not found")
        self._stop_process(state)
        physical = self._to_physical(state.config, state.value)
        command = [
            self._gpioset,
            "--mode=signal",
            "--consumer",
            self.config.consumer,
            state.config.chip,
            f"{state.config.line}={physical}",
        ]
        state.process = subprocess.Popen(command, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)

    def _prepare_sysfs(self, config: GpioLineConfig) -> None:
        base = self._sysfs_base(config)
        if not base.exists():
            export = Path("/sys/class/gpio/export")
            if not export.exists():
                raise RuntimeError("sysfs gpio export not available")
            export.write_text(str(config.number), encoding="ascii")
            for _ in range(20):
                if base.exists():
                    break
                time.sleep(0.05)
        direction = base / "direction"
        if direction.exists():
            direction.write_text(config.direction, encoding="ascii")
        active_low = base / "active_low"
        if active_low.exists():
            active_low.write_text("0", encoding="ascii")

    def _read_sysfs(self, config: GpioLineConfig) -> int:
        value = self._sysfs_base(config) / "value"
        physical = self._normalize(value.read_text(encoding="ascii").strip())
        return self._from_physical(config, physical)

    def _write_sysfs(self, config: GpioLineConfig, value: int) -> None:
        (self._sysfs_base(config) / "value").write_text(str(self._normalize(value)), encoding="ascii")

    def _sysfs_base(self, config: GpioLineConfig) -> Path:
        return Path("/sys/class/gpio") / f"gpio{config.number}"

    def _run_gpioget(self, config: GpioLineConfig) -> int:
        if not self._gpioget:
            raise RuntimeError("gpioget not found")
        result = subprocess.run(
            [self._gpioget, config.chip, str(config.line)],
            capture_output=True,
            text=True,
            check=True,
        )
        physical = self._normalize(result.stdout.strip())
        return self._from_physical(config, physical)

    def _stop_process(self, state: GpioState) -> None:
        if not state.process:
            return
        if state.process.poll() is None:
            state.process.terminate()
            try:
                state.process.wait(timeout=1)
            except subprocess.TimeoutExpired:
                state.process.kill()
        state.process = None

    def _to_physical(self, config: GpioLineConfig, value: int) -> int:
        return 0 if config.active_low and value else 1 if config.active_low else value

    def _from_physical(self, config: GpioLineConfig, value: int) -> int:
        return 0 if config.active_low and value else 1 if config.active_low else value

    def _normalize(self, value: object) -> int:
        if isinstance(value, str):
            value = value.strip().lower()
            return 1 if value in {"1", "true", "on", "high"} else 0
        return 1 if int(value) else 0
