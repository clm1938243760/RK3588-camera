from __future__ import annotations

import base64
import asyncio
import hashlib
import json
import logging
from datetime import datetime
from pathlib import Path
from typing import Any, Optional

from aiohttp import ClientSession, ClientTimeout

from .config import PatientApiConfig

LOGGER = logging.getLogger(__name__)
EXAM_ITEM_KEYS = ("exam_item", "exam_item_name", "examItemName", "examItem")
PATIENT_RESPONSE_FIELDS = (
    "birthday",
    "exam_item",
    "ming",
    "sex",
    "yue",
    "his_exam_no",
    "xing",
    "patient_id",
    "ri",
    "patient_name",
    "name_phonetic",
    "nian",
    "report_no",
    "age",
)


class PatientApiError(RuntimeError):
    """Sanitized patient API error safe to expose to local callers."""


def build_patient_sql(scan: str) -> str:
    kw = scan.replace("'", "''")
    return f"""select
  t.exam_item as exam_item,
  t.his_exam_no,
  z.report_no,
  t.patient_id,
  t.patient_name,
  q.name_phonetic,
  substr(t.patient_name, 0, 2) as xing,
  substr(t.patient_name, 2, 8) as ming,
  t.sex,
  t.age,
  to_char(t.birthday,'yyyy') as nian,
  to_char(t.birthday,'mm') as yue,
  to_char(t.birthday,'dd') as ri,
  t.birthday
from exam_master t
left join exam_item z on t.his_exam_no=z.his_exam_no
left join patient_info q on t.patient_id=q.patient_id
where
  (
    z.report_no like '%{kw}%'
    or t.patient_id like '%{kw}%'
    or t.patient_name like '%{kw}%'
  )
  and z.exam_state in ('10', '20', '30', '40')
  and t.req_date>= CURRENT_DATE - INTERVAL '180 days'
order by t.req_date desc
limit 20"""


def records_from_payload(payload: Any) -> list[dict[str, Any]]:
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [_normalize_record(item) for item in data if isinstance(item, dict)]
        if isinstance(data, dict):
            return [_normalize_record(data)]
        if payload.get("patient_id") or payload.get("patient_name"):
            return [_normalize_record(payload)]
    if isinstance(payload, list):
        return [_normalize_record(item) for item in payload if isinstance(item, dict)]
    return []


def canonical_patient_payload(payload: Any) -> dict[str, Any]:
    if isinstance(payload, dict):
        raw_data = payload.get("data", [])
        success = payload.get("success") is True
        code = str(payload.get("code") or ("SUCCESS" if success else "FAIL"))
        msg = str(payload.get("msg") or ("成功" if success else "失败"))
    elif isinstance(payload, list):
        raw_data = payload
        success = True
        code = "SUCCESS"
        msg = "成功"
    else:
        raise PatientApiError("patient api returned an unsupported payload")

    if isinstance(raw_data, dict):
        raw_records = [raw_data]
    elif isinstance(raw_data, list):
        raw_records = [item for item in raw_data if isinstance(item, dict)]
    elif raw_data is None:
        raw_records = []
    else:
        raise PatientApiError("patient api data is not an object or array")

    records = []
    for raw_record in raw_records:
        record = _normalize_record(raw_record)
        records.append({field: record.get(field) for field in PATIENT_RESPONSE_FIELDS})
    return {
        "code": code,
        "data": records,
        "msg": msg,
        "success": success,
    }


def failed_patient_payload(message: str = "患者查询失败") -> dict[str, Any]:
    return {
        "code": "FAIL",
        "data": [],
        "msg": message,
        "success": False,
    }


def _normalize_record(record: dict[str, Any]) -> dict[str, Any]:
    normalized = dict(record)
    if str(normalized.get("exam_item", "") or "").strip():
        return normalized
    for key in EXAM_ITEM_KEYS:
        value = normalized.get(key)
        if str(value or "").strip():
            normalized["exam_item"] = str(value).strip()
            break
    return normalized


def first_record(payload: Any) -> Optional[dict[str, Any]]:
    records = records_from_payload(payload)
    if records:
        return records[0]
    return None


class PatientApiClient:
    def __init__(self, config: PatientApiConfig) -> None:
        self.config = config
        self.raw_dir = Path(config.raw_dir)
        self.raw_dir.mkdir(parents=True, exist_ok=True)

    async def query_first(self, scan: str) -> Optional[dict[str, Any]]:
        records = await self.query_records(scan)
        return records[0] if records else None

    async def query_records(self, scan: str) -> list[dict[str, Any]]:
        try:
            payload = await self.query_payload(scan, persist_raw=True)
        except asyncio.CancelledError:
            raise
        except PatientApiError as exc:
            LOGGER.warning(
                "patient api query failed ref=%s reason=%s",
                _scan_reference(scan),
                str(exc),
            )
            return []
        records = records_from_payload(payload)
        if records:
            LOGGER.info(
                "patient api returned %d record(s) ref=%s",
                len(records),
                _scan_reference(scan),
            )
        else:
            LOGGER.warning("patient api returned no usable record ref=%s", _scan_reference(scan))
        return records

    async def query_payload(self, scan: str, persist_raw: bool = False) -> dict[str, Any]:
        if not self.config.enabled:
            raise PatientApiError("patient api is disabled")
        if not self.config.endpoint:
            raise PatientApiError("patient api endpoint is empty")

        sql = build_patient_sql(scan)
        request_body = {"sqlStr": base64.b64encode(sql.encode("utf-8")).decode("ascii")}
        headers = {
            "Content-Type": "application/json;charset=UTF-8",
            "Accept": "application/json",
            "User-Agent": self.config.user_agent,
        }
        timeout = ClientTimeout(total=self.config.timeout_seconds)
        LOGGER.info("patient api request ref=%s endpoint=%s", _scan_reference(scan), self.config.endpoint)

        try:
            async with ClientSession(timeout=timeout, headers=headers) as session:
                async with session.post(self.config.endpoint, json=request_body) as resp:
                    text = await resp.text()
                    if persist_raw:
                        raw_path = self._save_raw(scan, resp.status, text)
                        LOGGER.info("patient api raw response saved: %s", raw_path)
                    if not 200 <= resp.status < 300:
                        raise PatientApiError("patient api returned HTTP %s" % resp.status)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            raise PatientApiError("patient api request timed out") from None
        except PatientApiError:
            raise
        except Exception as exc:
            raise PatientApiError("patient api request failed: %s" % type(exc).__name__) from exc

        try:
            payload = json.loads(text)
        except json.JSONDecodeError as exc:
            raise PatientApiError("patient api returned invalid JSON") from exc
        return canonical_patient_payload(payload)

    def _save_raw(self, scan: str, status: int, text: str) -> Path:
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_scan = "".join(ch if ch.isalnum() else "_" for ch in scan)[:80]
        path = self.raw_dir / f"api_{stamp}_{status}_{safe_scan}.json"
        path.write_text(text, encoding="utf-8")
        return path


def _scan_reference(scan: str) -> str:
    return hashlib.sha256(scan.encode("utf-8")).hexdigest()[:12]
