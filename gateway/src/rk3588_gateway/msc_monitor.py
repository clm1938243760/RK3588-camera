from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import shutil
import subprocess
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional, Union

from .compat import to_thread
from .config import MscConfig
from .events import GatewayEvent
from .printer import Printer
from .queue import EventQueue
from .report_pdf import ReportPdfConverter

LOGGER = logging.getLogger(__name__)


class MscMonitor:
    def __init__(
        self,
        config: MscConfig,
        queue: EventQueue,
        device_id: str,
        report_pdf: Optional[ReportPdfConverter] = None,
        printer: Optional[Printer] = None,
        defer_reads: Optional[Callable[[], bool]] = None,
        before_gadget_unbind: Optional[Callable[[], bool]] = None,
        after_gadget_rebind: Optional[Callable[[], None]] = None,
    ) -> None:
        self.config = config
        self.queue = queue
        self.device_id = device_id
        self.report_pdf = report_pdf
        self.printer = printer
        self.defer_reads = defer_reads
        self.before_gadget_unbind = before_gadget_unbind
        self.after_gadget_rebind = after_gadget_rebind
        self.image_path = Path(config.image_path)
        self.mount_dir = Path(config.mount_dir)
        self.output_dir = Path(config.output_dir)
        self.state_dir = Path(config.state_dir)
        self.seen_db = self.state_dir / "seen.db"
        self.records_file = self.state_dir / "files.jsonl"
        self.last_mtime_file = self.state_dir / "last_mtime"
        self._stop = asyncio.Event()
        self._deferred_mtime = ""

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        if not self.config.enabled:
            LOGGER.info("msc monitor disabled")
            return

        self.mount_dir.mkdir(parents=True, exist_ok=True)
        self.output_dir.mkdir(parents=True, exist_ok=True)
        self.state_dir.mkdir(parents=True, exist_ok=True)
        self.seen_db.touch(exist_ok=True)
        self.records_file.touch(exist_ok=True)
        LOGGER.info("msc monitor watching image: %s", self.image_path)

        while not self._stop.is_set():
            if not self.image_path.exists():
                LOGGER.warning("waiting for msc image: %s", self.image_path)
                await asyncio.sleep(self.config.poll_interval_seconds)
                continue

            current_mtime = self._image_mtime()
            previous_mtime = self._read_last_mtime()
            if current_mtime and not previous_mtime and self.config.init_baseline:
                self._write_last_mtime(current_mtime)
                LOGGER.info("msc init baseline mtime=%s", current_mtime)
                await asyncio.sleep(self.config.poll_interval_seconds)
                continue

            if current_mtime and current_mtime != previous_mtime:
                if self._defer_read(current_mtime, "mtime changed"):
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue
                stable_mtime = await self._wait_host_quiet(current_mtime)
                if not stable_mtime:
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue
                if self._defer_read(stable_mtime, "host quiet"):
                    await asyncio.sleep(self.config.poll_interval_seconds)
                    continue
                try:
                    copied = await to_thread(self._copy_new_files)
                    if copied is None:
                        await asyncio.sleep(self.config.poll_interval_seconds)
                        continue
                    self._write_last_mtime(self._image_mtime() or stable_mtime)
                    if copied:
                        LOGGER.info("msc copied %d new file(s)", len(copied))
                except Exception:
                    LOGGER.exception("msc monitor cycle failed")

            await asyncio.sleep(self.config.poll_interval_seconds)

    def _image_mtime(self) -> str:
        try:
            return str(self.image_path.stat().st_mtime_ns)
        except FileNotFoundError:
            return ""

    def _read_last_mtime(self) -> str:
        try:
            return self.last_mtime_file.read_text(encoding="utf-8").strip()
        except FileNotFoundError:
            return ""

    def _write_last_mtime(self, value: str) -> None:
        self.last_mtime_file.write_text(value, encoding="utf-8")

    def _defer_read(self, mtime: str, stage: str) -> bool:
        if not self.defer_reads:
            self._deferred_mtime = ""
            return False
        try:
            deferred = bool(self.defer_reads())
        except Exception:
            LOGGER.exception("msc defer predicate failed")
            deferred = False
        if not deferred:
            self._deferred_mtime = ""
            return False
        if self._deferred_mtime != mtime:
            LOGGER.info("msc read deferred during hid input stage=%s mtime=%s", stage, mtime)
            self._deferred_mtime = mtime
        return True

    async def _wait_host_quiet(self, first_mtime: str) -> str:
        last_mtime = first_mtime
        quiet_started = time.monotonic()

        while not self._stop.is_set():
            await asyncio.sleep(self.config.stable_seconds)
            current_mtime = self._image_mtime()
            if not current_mtime:
                return ""
            if current_mtime != last_mtime:
                LOGGER.info("msc host still writing old=%s new=%s", last_mtime, current_mtime)
                last_mtime = current_mtime
                quiet_started = time.monotonic()
                continue
            if time.monotonic() - quiet_started >= self.config.quiet_seconds:
                LOGGER.info("msc host quiet, detach UDC before local read mtime=%s", current_mtime)
                return current_mtime

        return ""

    def _copy_new_files(self) -> Optional[list[Path]]:
        if self._defer_read(self._image_mtime(), "before unbind"):
            return None
        if not self._prepare_gadget_unbind():
            LOGGER.warning("msc skip local read because printer capture did not close before UDC unbind")
            return None
        udc = self._unbind_gadget()
        copied: list[Path] = []
        pdfs_to_print = []
        try:
            self._detach_mass_storage_file()
            self._mount_image_ro()
            records = self._load_records()
            files = self._iter_files()
            LOGGER.info("msc mounted image contains %d visible file(s)", len(files))
            skipped = 0
            for source in files:
                info = self._file_info(source)
                signature = str(info["signature"])
                LOGGER.info(
                    "msc inspect file rel=%s size=%s mtime_ns=%s sha256=%s",
                    info["rel"],
                    info["size"],
                    info["mtime_ns"],
                    str(info["sha256"])[:16],
                )
                if signature in records:
                    skipped += 1
                    LOGGER.info("msc old file skip rel=%s signature=%s", info["rel"], signature[:24])
                    continue
                LOGGER.info("msc new file copy rel=%s signature=%s", info["rel"], signature[:24])
                target = self._copy_file(source)
                copied.append(target)
                records.add(signature)
                self._append_record(info, target)
                self.queue.put(
                    GatewayEvent(
                        type="msc.file_copied",
                        device_id=self.device_id,
                        payload={"source": str(source), "path": str(target), "bytes": target.stat().st_size},
                    )
                )
                if self.report_pdf:
                    pdf_path = self.report_pdf.convert(target, "msc")
                    if pdf_path:
                        self.queue.put(
                            GatewayEvent(
                                type="report.pdf_created",
                                device_id=self.device_id,
                                payload={"source": str(target), "path": str(pdf_path), "source_type": "msc"},
                            )
                        )
                        pdfs_to_print.append(pdf_path)
            if skipped:
                LOGGER.info("msc skipped %d already copied file(s)", skipped)
            subprocess.run(["sync"], check=False)
        finally:
            self._umount_image()
            if not self._rebuild_gadget():
                self._attach_mass_storage_file()
                self._bind_gadget(udc)
            self._notify_gadget_rebound()
        for pdf_path in pdfs_to_print:
            self._print_pdf(pdf_path, "msc report")
        return copied

    def _prepare_gadget_unbind(self) -> bool:
        if not self.before_gadget_unbind:
            return True
        try:
            return bool(self.before_gadget_unbind())
        except Exception:
            LOGGER.exception("before gadget unbind callback failed")
            return False

    def _notify_gadget_rebound(self) -> None:
        if not self.after_gadget_rebind:
            return
        try:
            self.after_gadget_rebind()
        except Exception:
            LOGGER.exception("after gadget rebind callback failed")

    def _iter_files(self) -> list[Path]:
        iterator = self.mount_dir.rglob("*") if self.config.copy_recursive else self.mount_dir.glob("*")
        files: list[Path] = []
        ignore = set(self.config.ignore_names)
        for path in iterator:
            if not path.is_file():
                continue
            rel_parts = path.relative_to(self.mount_dir).parts
            if any(part in ignore for part in rel_parts):
                continue
            if path.name.startswith("~$"):
                continue
            files.append(path)
        return files

    def _copy_file(self, source: Path) -> Path:
        rel = source.relative_to(self.mount_dir)
        target = self.output_dir / rel
        if target.exists():
            stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
            target = target.with_name(f"{target.stem}_{stamp}{target.suffix}")
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(source, target)
        LOGGER.info("msc file copied: %s -> %s", source, target)
        return target

    def _file_info(self, path: Path) -> dict[str, Union[str, int]]:
        rel = path.relative_to(self.mount_dir).as_posix()
        stat = path.stat()
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        sha256 = digest.hexdigest()
        return {
            "rel": rel,
            "size": stat.st_size,
            "mtime_ns": stat.st_mtime_ns,
            "sha256": sha256,
            "signature": f"{rel}|{stat.st_size}|{sha256}",
        }

    def _load_records(self) -> set[str]:
        records = {
            line.strip()
            for line in self.seen_db.read_text(encoding="utf-8").splitlines()
            if line.strip()
        }
        for line in self.records_file.read_text(encoding="utf-8").splitlines():
            if not line.strip():
                continue
            try:
                item = json.loads(line)
            except json.JSONDecodeError:
                continue
            signature = str(item.get("signature", "")).strip()
            if signature:
                records.add(signature)
        return records

    def _append_record(self, info: dict[str, Union[str, int]], target: Path) -> None:
        signature = str(info["signature"])
        with self.seen_db.open("a", encoding="utf-8") as handle:
            handle.write(signature + "\n")
        record = {
            **info,
            "copied_to": str(target),
            "copied_at": datetime.now().isoformat(timespec="seconds"),
        }
        with self.records_file.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(record, ensure_ascii=False) + "\n")

    def _mass_storage_file_attr(self) -> Path:
        functions = Path(self.config.gadget_dir) / "functions"
        for name in ("mass_storage.0", "mass_storage.usb0"):
            attr = functions / name / "lun.0" / "file"
            if attr.exists():
                return attr
        return functions / "mass_storage.0" / "lun.0" / "file"

    def _udc_attr(self) -> Path:
        return Path(self.config.gadget_dir) / "UDC"

    def _unbind_gadget(self) -> str:
        udc_attr = self._udc_attr()
        current = ""
        if udc_attr.exists():
            current = udc_attr.read_text(encoding="utf-8").strip()
            try:
                udc_attr.write_text("", encoding="utf-8")
                time.sleep(1)
            except OSError:
                LOGGER.exception("failed to unbind UDC")
        return current or self.config.udc_device or self._discover_udc()

    def _bind_gadget(self, udc: str) -> None:
        if not udc:
            LOGGER.warning("no UDC available for msc rebind")
            return
        udc_attr = self._udc_attr()
        try:
            current = udc_attr.read_text(encoding="utf-8").strip() if udc_attr.exists() else ""
        except OSError:
            current = ""
        if current == udc:
            LOGGER.info("usb gadget already bound: %s", udc)
            return
        try:
            udc_attr.write_text(udc, encoding="utf-8")
            LOGGER.info("usb gadget rebound: %s", udc)
        except OSError:
            owner = self._find_udc_owner(udc)
            if owner == str(Path(self.config.gadget_dir)):
                LOGGER.info("usb gadget already bound after bind race: %s", udc)
                return
            LOGGER.exception("failed to bind UDC: %s owner=%s", udc, owner or "unknown")

    def _find_udc_owner(self, udc: str) -> str:
        root = Path("/sys/kernel/config/usb_gadget")
        try:
            for attr in sorted(root.glob("*/UDC")):
                try:
                    if attr.read_text(encoding="utf-8").strip() == udc:
                        return str(attr.parent)
                except OSError:
                    continue
        except OSError:
            return ""
        return ""

    def _discover_udc(self) -> str:
        udc_dir = Path("/sys/class/udc")
        try:
            return next(iter(sorted(path.name for path in udc_dir.iterdir())))
        except (FileNotFoundError, StopIteration):
            return ""

    def _detach_mass_storage_file(self) -> None:
        attr = self._mass_storage_file_attr()
        if attr.exists():
            try:
                attr.write_text("", encoding="utf-8")
                time.sleep(0.3)
            except OSError:
                LOGGER.exception("failed to detach msc backing file")

    def _attach_mass_storage_file(self) -> None:
        attr = self._mass_storage_file_attr()
        if attr.exists():
            try:
                attr.write_text(str(self.image_path), encoding="utf-8")
            except OSError:
                LOGGER.exception("failed to attach msc backing file")

    def _mount_image_ro(self) -> None:
        if self._is_mounted():
            return
        subprocess.run(["mount", "-o", "loop,ro", str(self.image_path), str(self.mount_dir)], check=True)

    def _umount_image(self) -> None:
        if self._is_mounted():
            subprocess.run(["umount", str(self.mount_dir)], check=False)

    def _is_mounted(self) -> bool:
        mount_dir = str(self.mount_dir)
        try:
            with Path("/proc/mounts").open("r", encoding="utf-8") as handle:
                return any(line.split()[1] == mount_dir for line in handle if line.split())
        except FileNotFoundError:
            return False

    def _rebuild_gadget(self) -> bool:
        if not self.config.rebuild_command:
            return False
        command = Path(self.config.rebuild_command)
        if not command.exists():
            LOGGER.warning("msc rebuild command missing: %s", command)
            return False

        LOGGER.info("msc rebuild usb gadget: %s", command)
        result = subprocess.run(["/bin/bash", str(command)], capture_output=True, text=True)
        if result.returncode != 0:
            LOGGER.error("msc rebuild failed: %s", result.stderr.strip())
            return False
        if result.stdout.strip():
            LOGGER.info("msc rebuild output: %s", result.stdout.strip()[-500:])
        return True

    def _print_pdf(self, path: Path, title: str) -> None:
        if not self.printer:
            return
        try:
            ok = self.printer.print_file_blocking(path, title=title)
            self.queue.put(
                GatewayEvent(
                    type="report.printed" if ok else "report.print_failed",
                    device_id=self.device_id,
                    payload={"path": str(path), "source_type": "msc"},
                )
            )
        except Exception:
            LOGGER.exception("print converted pdf failed: %s", path)
