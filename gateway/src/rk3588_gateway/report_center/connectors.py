from __future__ import annotations

import asyncio
import base64
import json
import re
from typing import Any, Optional
from urllib.parse import urlencode

from aiohttp import ClientSession, ClientTimeout

from .domain import STANDARD_PATIENT_FIELDS, ValidationError, canonical_patient


DEFAULT_PATIENT_SQL = """SELECT
birth_day AS birthday,
exam_item_name AS exam_item,
name_second AS ming,
patient_sex AS sex,
birth_month AS yue,
exam_no AS his_exam_no,
name_first AS xing,
patient_id,
birth_date AS ri,
patient_name,
name_phonetic,
birth_year AS nian,
report_no,
patient_age AS age
FROM patient_exam_view
WHERE report_no={{query_literal}}
   OR patient_id={{query_literal}}
   OR patient_name={{query_literal}}"""

_TEMPLATE_PATTERN = re.compile(r"{{\s*([a-zA-Z0-9_]+)\s*}}")
_JSON_PATH_PART = re.compile(r"(?:^|\.)([A-Za-z_][A-Za-z0-9_]*)|\[(\d+)\]")


class ConnectorError(RuntimeError):
    pass


class PatientConnectorService:
    async def query(self, connector: dict[str, Any], query: str) -> list[dict[str, Any]]:
        query = _validate_query(query)
        connector_type = str(connector.get("type", ""))
        config = connector.get("config", {})
        if not isinstance(config, dict):
            raise ConnectorError("connector configuration is invalid")
        if connector_type == "sql_proxy":
            payload = await _query_sql_proxy(config, query)
        elif connector_type == "rest_json":
            payload = await _query_rest_json(config, query)
        else:
            raise ConnectorError("connector is not a patient connector")
        return _map_records(payload, config)

    async def test(self, connector: dict[str, Any], query: str) -> dict[str, Any]:
        started = asyncio.get_running_loop().time()
        records = await self.query(connector, query)
        elapsed = round((asyncio.get_running_loop().time() - started) * 1000, 1)
        return {
            "ok": True,
            "record_count": len(records),
            "fields": sorted({key for record in records for key in record if key != "extra_fields"}),
            "elapsed_ms": elapsed,
        }


async def _query_sql_proxy(config: dict[str, Any], query: str) -> Any:
    endpoint = _required_endpoint(config)
    sql_template = str(config.get("sql_template") or DEFAULT_PATIENT_SQL)
    sql = render_sql_template(sql_template, query)
    body = {str(config.get("request_field", "sqlStr")): base64.b64encode(sql.encode("utf-8")).decode("ascii")}
    return await _request_json("POST", endpoint, config, body=body)


async def _query_rest_json(config: dict[str, Any], query: str) -> Any:
    endpoint = _required_endpoint(config)
    method = str(config.get("method", "POST")).upper()
    if method not in {"GET", "POST"}:
        raise ValidationError("REST patient connector only supports GET or POST")
    request_template = config.get("request", {"code": "{{query}}"})
    request_payload = _render_json_template(request_template, query)
    if method == "GET":
        if not isinstance(request_payload, dict):
            raise ValidationError("GET request template must be an object")
        endpoint += ("&" if "?" in endpoint else "?") + urlencode(request_payload)
        return await _request_json(method, endpoint, config)
    return await _request_json(method, endpoint, config, body=request_payload)


async def _request_json(method: str, endpoint: str, config: dict[str, Any], body: Any = None) -> Any:
    timeout = ClientTimeout(total=max(1, int(config.get("timeout_seconds", 10))))
    headers = config.get("headers", {})
    if not isinstance(headers, dict):
        raise ValidationError("connector headers must be an object")
    safe_headers = {str(key): str(value) for key, value in headers.items()}
    safe_headers.setdefault("User-Agent", "RK3588-Report-Center")
    try:
        async with ClientSession(timeout=timeout, headers=safe_headers) as session:
            async with session.request(method, endpoint, json=body) as response:
                text = await response.text()
                if not 200 <= response.status < 300:
                    raise ConnectorError("patient connector returned HTTP %d" % response.status)
    except asyncio.TimeoutError:
        raise ConnectorError("patient connector timed out") from None
    except ConnectorError:
        raise
    except Exception as exc:
        raise ConnectorError("patient connector request failed: %s" % type(exc).__name__) from exc
    try:
        return json.loads(text)
    except json.JSONDecodeError as exc:
        raise ConnectorError("patient connector returned invalid JSON") from exc


def render_sql_template(template: str, query: str) -> str:
    names = set(_TEMPLATE_PATTERN.findall(template))
    unknown = names - {"query_literal", "query_like"}
    if unknown:
        raise ValidationError("unsupported SQL placeholder: %s" % sorted(unknown)[0])
    if not names:
        raise ValidationError("SQL template must contain a typed query placeholder")
    escaped = query.replace("'", "''")
    result = _TEMPLATE_PATTERN.sub(
        lambda match: "'%" + escaped + "%'" if match.group(1) == "query_like" else "'" + escaped + "'",
        template,
    )
    if "{{" in result or "}}" in result:
        raise ValidationError("SQL template contains an invalid placeholder")
    return result


def _map_records(payload: Any, config: dict[str, Any]) -> list[dict[str, Any]]:
    records_path = str(config.get("records_path", "$.data"))
    raw_records = json_path_get(payload, records_path)
    if isinstance(raw_records, dict):
        records = [raw_records]
    elif isinstance(raw_records, list):
        records = [item for item in raw_records if isinstance(item, dict)]
    elif raw_records is None:
        records = []
    else:
        raise ConnectorError("patient connector records path is not an object or array")

    mapping = config.get("field_mapping", {})
    if mapping and not isinstance(mapping, dict):
        raise ValidationError("field_mapping must be an object")
    result = []
    for raw in records:
        if mapping:
            patient: dict[str, Any] = {}
            extra: dict[str, Any] = {}
            for target, path in mapping.items():
                value = json_path_get(raw, str(path))
                if target in STANDARD_PATIENT_FIELDS:
                    patient[str(target)] = value
                else:
                    extra[str(target)] = value
            patient["extra_fields"] = extra
        else:
            patient = dict(raw)
        result.append(canonical_patient(patient))
    return result


def json_path_get(payload: Any, path: str) -> Any:
    if path in {"", "$"}:
        return payload
    if not path.startswith("$"):
        raise ValidationError("JSONPath must start with $")
    cursor = payload
    consumed = 1
    for match in _JSON_PATH_PART.finditer(path[1:]):
        if match.start() != consumed - 1:
            raise ValidationError("unsupported JSONPath")
        key, index = match.groups()
        if key is not None:
            if not isinstance(cursor, dict):
                return None
            cursor = cursor.get(key)
        else:
            if not isinstance(cursor, list) or int(index) >= len(cursor):
                return None
            cursor = cursor[int(index)]
        consumed = match.end() + 1
    if consumed != len(path):
        raise ValidationError("unsupported JSONPath")
    return cursor


def _render_json_template(value: Any, query: str) -> Any:
    if isinstance(value, dict):
        return {str(key): _render_json_template(item, query) for key, item in value.items()}
    if isinstance(value, list):
        return [_render_json_template(item, query) for item in value]
    if isinstance(value, str):
        names = set(_TEMPLATE_PATTERN.findall(value))
        if names - {"query"}:
            raise ValidationError("unsupported REST request placeholder")
        return _TEMPLATE_PATTERN.sub(query, value)
    if value is None or isinstance(value, (bool, int, float)):
        return value
    raise ValidationError("REST request template contains an unsupported value")


def _validate_query(query: str) -> str:
    value = str(query).strip()
    if not 1 <= len(value) <= 128 or any(ord(char) < 32 for char in value):
        raise ValidationError("patient query is invalid")
    return value


def _required_endpoint(config: dict[str, Any]) -> str:
    endpoint = str(config.get("endpoint", "")).strip()
    if not endpoint.startswith(("http://", "https://")):
        raise ValidationError("connector endpoint must be HTTP or HTTPS")
    return endpoint
