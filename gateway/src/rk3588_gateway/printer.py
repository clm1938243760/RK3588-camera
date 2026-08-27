from __future__ import annotations

import asyncio
import logging
import subprocess
from pathlib import Path
from tempfile import NamedTemporaryFile

from .config import PrinterConfig
from .compat import unlink_missing_ok

LOGGER = logging.getLogger(__name__)


class Printer:
    def __init__(self, config: PrinterConfig) -> None:
        self.config = config

    async def print_text(self, text: str, title: str = "rk3588-gateway") -> bool:
        if not self.config.enabled:
            LOGGER.info("printer disabled, skipped print job: %s", title)
            return False

        with NamedTemporaryFile("w", encoding="utf-8", delete=False, suffix=".txt") as handle:
            handle.write(text)
            temp_path = Path(handle.name)

        try:
            return await self.print_file(temp_path, title=title)
        finally:
            unlink_missing_ok(temp_path)

    async def print_file(self, path: Path, title: str = "rk3588-gateway") -> bool:
        if not self.config.enabled:
            LOGGER.info("printer disabled, skipped file: %s", path)
            return False

        command = self._command(path, title)
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await asyncio.wait_for(
            proc.communicate(),
            timeout=self.config.timeout_seconds,
        )
        if proc.returncode == 0:
            LOGGER.info("print submitted: %s", stdout.decode(errors="replace").strip())
            return True
        LOGGER.error("print failed: %s", stderr.decode(errors="replace").strip())
        return False

    def print_file_blocking(self, path: Path, title: str = "rk3588-gateway") -> bool:
        if not self.config.enabled:
            LOGGER.info("printer disabled, skipped file: %s", path)
            return False

        result = subprocess.run(
            self._command(path, title),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=self.config.timeout_seconds,
        )
        if result.returncode == 0:
            LOGGER.info("print submitted: %s", result.stdout.decode(errors="replace").strip())
            return True
        LOGGER.error("print failed: %s", result.stderr.decode(errors="replace").strip())
        return False

    def _command(self, path: Path, title: str) -> list[str]:
        command = [self.config.command]
        if self.config.printer_name:
            command.extend(["-d", self.config.printer_name])
        command.extend(["-t", title, str(path)])
        return command
