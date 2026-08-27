from __future__ import annotations

import asyncio
import logging
from pathlib import Path
from typing import Union

from .compat import unlink_missing_ok
from .config import VmTransferConfig

LOGGER = logging.getLogger(__name__)


class VmTransfer:
    def __init__(self, config: VmTransferConfig) -> None:
        self.config = config

    async def send_file(self, path: Union[str, Path]) -> bool:
        if not self.config.enabled:
            LOGGER.info("vm transfer disabled")
            return False
        if self.config.method != "scp":
            LOGGER.error("unsupported vm transfer method: %s", self.config.method)
            return False
        if not self.config.host or not self.config.user:
            LOGGER.error("vm transfer host/user is empty")
            return False

        source = Path(path)
        if not source.exists():
            LOGGER.error("vm transfer source not found: %s", source)
            return False

        if not await self._ensure_remote_dir():
            return False

        target = f"{self.config.user}@{self.config.host}:{self.config.remote_dir.rstrip('/')}/"
        command = self._with_password(
            [
            "scp",
            "-P",
            str(self.config.port),
            "-o",
            f"ConnectTimeout={self.config.connect_timeout_seconds}",
            "-o",
            "StrictHostKeyChecking=accept-new",
            "-o",
            "PreferredAuthentications=password,publickey",
            str(source),
            target,
            ]
        )
        LOGGER.info("vm transfer start: %s -> %s", source, target)
        return await self._run_transfer(command, source)

    async def _ensure_remote_dir(self) -> bool:
        command = self._with_password(
            [
                "ssh",
                "-p",
                str(self.config.port),
                "-o",
                f"ConnectTimeout={self.config.connect_timeout_seconds}",
                "-o",
                "StrictHostKeyChecking=accept-new",
                "-o",
                "PreferredAuthentications=password,publickey",
                f"{self.config.user}@{self.config.host}",
                f"mkdir -p {self.config.remote_dir}",
            ]
        )
        LOGGER.info("vm transfer ensure remote dir: %s", self.config.remote_dir)
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            return True
        LOGGER.error(
            "vm remote mkdir failed rc=%s stdout=%s stderr=%s",
            proc.returncode,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )
        return False

    async def _run_transfer(self, command: list[str], source: Path) -> bool:
        proc = await asyncio.create_subprocess_exec(
            *command,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, stderr = await proc.communicate()
        if proc.returncode == 0:
            LOGGER.info("vm transfer done: %s", source)
            if not self.config.keep_local_copy:
                unlink_missing_ok(source)
            return True

        LOGGER.error(
            "vm transfer failed rc=%s stdout=%s stderr=%s",
            proc.returncode,
            stdout.decode(errors="replace").strip(),
            stderr.decode(errors="replace").strip(),
        )
        return False

    def _with_password(self, command: list[str]) -> list[str]:
        if self.config.password:
            return ["sshpass", "-p", self.config.password, *command]
        return command
