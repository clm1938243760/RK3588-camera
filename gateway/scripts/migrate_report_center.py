#!/usr/bin/env python3
from __future__ import annotations

import argparse
import hashlib
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))

from rk3588_gateway.config import load_config
from rk3588_gateway.report_center.archive import ReportArchive
from rk3588_gateway.report_center.config import load_report_center_config
from rk3588_gateway.report_center.store import ReportCenterStore


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Import legacy RK3588 reports without guessing patient links")
    parser.add_argument("--config", default=str(ROOT / "config.yaml"))
    parser.add_argument("--execute", action="store_true", help="perform the import; default is a dry run")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    app_config = load_config(args.config)
    center_config = load_report_center_config(args.config)
    roots = {
        Path(app_config.report_pdf.output_dir),
        Path(app_config.msc.output_dir),
        Path(app_config.print_capture.output_dir),
    }
    candidates = sorted({path.resolve() for root in roots if root.exists() for path in root.rglob("*.pdf") if path.is_file()})
    if not args.execute:
        print(json.dumps({
            "dry_run": True,
            "pdf_candidates": len(candidates),
            "legacy_record_counts": _count_legacy_records(app_config),
            "note": "no database, archive or legacy files were changed",
        }, ensure_ascii=False, indent=2))
        return
    store = ReportCenterStore(center_config.database_path)
    archive = ReportArchive(center_config, store)
    imported = 0
    skipped = 0
    failed = []
    for path in candidates:
        digest = sha256_file(path)
        if store.find_report_by_sha256(digest):
            skipped += 1
            continue
        source = "printer" if "print" in path.name.lower() else "msc"
        try:
            report = archive.ingest_pdf(path, source, None, actor="migration")
            store.set_orphan_reason(int(report["id"]), "legacy_orphan")
            imported += 1
        except Exception as exc:
            failed.append({"path": str(path), "error": str(exc)})
    legacy_records = _count_legacy_records(app_config)
    result = {
        "dry_run": False,
        "pdf_candidates": len(candidates),
        "already_imported": skipped,
        "imported": imported,
        "failed": failed,
        "legacy_record_counts": legacy_records,
        "note": "legacy upload records are retained read-only and imported PDFs are not automatically re-uploaded",
    }
    print(json.dumps(result, ensure_ascii=False, indent=2))


def _count_legacy_records(config) -> dict[str, int]:
    result = {}
    for name, path in (
        ("uploads_jsonl", Path(config.report_upload.state_dir) / "uploads.jsonl"),
        ("msc_jsonl", Path(config.msc.state_dir) / "files.jsonl"),
    ):
        if not path.is_file():
            result[name] = 0
            continue
        with path.open("r", encoding="utf-8", errors="replace") as handle:
            result[name] = sum(1 for line in handle if line.strip())
    events = Path(config.storage.sqlite_path)
    result["events_db_present"] = int(events.is_file())
    return result


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


if __name__ == "__main__":
    main()
