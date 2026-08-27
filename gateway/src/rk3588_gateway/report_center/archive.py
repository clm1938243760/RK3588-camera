from __future__ import annotations

import hashlib
import os
import shutil
import tempfile
from datetime import datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, Optional, Union

from .domain import ConflictError, ValidationError, safe_filename_part, validate_report_source
from .store import ReportCenterStore

if TYPE_CHECKING:
    from .config import ReportCenterConfig


class ReportArchive:
    def __init__(self, config: ReportCenterConfig, store: ReportCenterStore) -> None:
        self.config = config
        self.store = store
        self.archive_root = Path(config.archive_dir)
        self.incoming_root = Path(config.incoming_dir)
        for path in (self.archive_root, self.incoming_root):
            path.mkdir(parents=True, exist_ok=True)
            _chmod(path, 0o700)

    def ingest_pdf(
        self,
        source_path: Union[str, Path],
        source: str,
        session: Optional[dict[str, Any]],
        actor: str = "system",
        upload_target: Optional[dict[str, Any]] = None,
    ) -> dict[str, Any]:
        source = validate_report_source(source)
        original = Path(source_path)
        if not original.is_file():
            raise ValidationError("report source file does not exist")
        incoming = self._stage(original)
        report_id: Optional[int] = None
        try:
            size, digest = _validate_pdf(incoming)
            session_id = str(session["id"]) if session else None
            report_id = self.store.reserve_report(
                session_id, source, original.name, str(incoming), size, digest
            )
            archive_path = self._archive_path(session)
            archive_path.parent.mkdir(parents=True, exist_ok=True)
            _chmod(archive_path.parent, 0o700)
            os.replace(incoming, archive_path)
            _chmod(archive_path, 0o600)
            metadata = _report_metadata(session, source)
            report_info = _read_report_info(self.config.report_info_path)
            return self.store.finalize_report(
                report_id, str(archive_path), metadata, actor, upload_target, report_info
            )
        except Exception as exc:
            incoming.unlink(missing_ok=True)
            if report_id is not None:
                self.store.fail_report(report_id, str(exc))
            raise

    def ingest_batch(
        self,
        paths: list[Union[str, Path]],
        source: str,
        session: Optional[dict[str, Any]],
        actor: str = "system",
        upload_target: Optional[dict[str, Any]] = None,
    ) -> list[dict[str, Any]]:
        if len(paths) == 1:
            return [self.ingest_pdf(paths[0], source, session, actor, upload_target)]
        results = []
        for path in paths:
            results.append(self.ingest_pdf(path, source, None, actor, None))
        return results

    def cleanup(self, retention_days: int, execute: bool = False) -> dict[str, Any]:
        candidates = self.store.cleanup_preview(retention_days)
        removed: list[int] = []
        errors: list[dict[str, Any]] = []
        if execute:
            for item in candidates:
                path = Path(str(item["archive_path"]))
                try:
                    path.unlink(missing_ok=True)
                    removed.append(int(item["id"]))
                except OSError as exc:
                    errors.append({"id": item["id"], "error": str(exc)})
            self.store.mark_purged(removed)
        return {
            "retention_days": retention_days,
            "candidate_count": len(candidates),
            "candidate_bytes": sum(int(item["size"]) for item in candidates),
            "removed_count": len(removed),
            "errors": errors,
            "items": candidates,
        }

    def _stage(self, source: Path) -> Path:
        fd, name = tempfile.mkstemp(prefix="report-", suffix=".pdf", dir=str(self.incoming_root))
        try:
            with os.fdopen(fd, "wb") as target, source.open("rb") as handle:
                shutil.copyfileobj(handle, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            _chmod(Path(name), 0o600)
            return Path(name)
        except Exception:
            try:
                os.close(fd)
            except OSError:
                pass
            Path(name).unlink(missing_ok=True)
            raise

    def _archive_path(self, session: Optional[dict[str, Any]]) -> Path:
        now = datetime.now()
        patient = session.get("patient", {}) if session else {}
        name = safe_filename_part(patient.get("patient_name") if isinstance(patient, dict) else "", "未知患者")
        directory = self.archive_root / now.strftime("%Y") / now.strftime("%m") / now.strftime("%d")
        stem = "%s_%s" % (name, now.strftime("%Y%m%d_%H%M%S"))
        candidate = directory / (stem + ".pdf")
        suffix = 2
        while candidate.exists():
            candidate = directory / ("%s_%d.pdf" % (stem, suffix))
            suffix += 1
        return candidate


def _validate_pdf(path: Path) -> tuple[int, str]:
    stat = path.stat()
    if stat.st_size < 5:
        raise ValidationError("report PDF is empty")
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        if handle.read(5) != b"%PDF-":
            raise ValidationError("report is not a PDF")
        handle.seek(0)
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return stat.st_size, digest.hexdigest()


def _report_metadata(session: Optional[dict[str, Any]], source: str) -> dict[str, Any]:
    return {
        "patient": session.get("patient") if session else None,
        "session_id": session.get("id") if session else None,
        "exam_item": session.get("exam_item", "") if session else "",
        "source": source,
        "notes": "",
    }


def _read_report_info(path: str) -> bytes:
    report_info = Path(path)
    return report_info.read_bytes() if report_info.is_file() else b""


def _chmod(path: Path, mode: int) -> None:
    try:
        os.chmod(path, mode)
    except OSError:
        pass
