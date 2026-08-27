from __future__ import annotations

import asyncio
import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import ModuleType, SimpleNamespace


SRC = Path(__file__).resolve().parents[1] / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))


class FakeWebResponse:
    def __init__(self, payload, status=200) -> None:
        self.status = status
        self.text = json.dumps(payload, ensure_ascii=False)


class FakeApplication:
    def add_routes(self, routes) -> None:
        self.routes = list(routes)


web_stub = SimpleNamespace(
    Application=FakeApplication,
    AppRunner=object,
    TCPSite=object,
    Request=object,
    Response=FakeWebResponse,
    get=lambda path, handler: ("GET", path, handler),
    post=lambda path, handler: ("POST", path, handler),
    json_response=lambda payload, status=200: FakeWebResponse(payload, status),
)
aiohttp_stub = ModuleType("aiohttp")
aiohttp_stub.ClientSession = object
aiohttp_stub.ClientTimeout = lambda **kwargs: None
aiohttp_stub.web = web_stub
sys.modules.setdefault("aiohttp", aiohttp_stub)

yaml_stub = ModuleType("yaml")
yaml_stub.safe_load = lambda handle: {}
sys.modules.setdefault("yaml", yaml_stub)

from rk3588_gateway import api as api_module
from rk3588_gateway.api import LocalApi
from rk3588_gateway.patient_api import (
    PATIENT_RESPONSE_FIELDS,
    PatientApiError,
    canonical_patient_payload,
)


def patient_config(raw_dir: str):
    return SimpleNamespace(
        enabled=True,
        endpoint="http://patient.invalid/query",
        timeout_seconds=1,
        user_agent="test",
        raw_dir=raw_dir,
    )


class FakeRequest:
    def __init__(self, payload, remote="127.0.0.1") -> None:
        self.payload = payload
        self.remote = remote

    async def json(self):
        if isinstance(self.payload, Exception):
            raise self.payload
        return self.payload


class FakePatientClient:
    response = None
    error = None
    calls = []

    def __init__(self, config) -> None:
        self.config = config

    async def query_payload(self, code, persist_raw=False):
        self.__class__.calls.append((code, persist_raw))
        if self.__class__.error is not None:
            raise self.__class__.error
        return self.__class__.response


class PatientPayloadTests(unittest.TestCase):
    def test_canonical_payload_keeps_all_records_and_exact_field_names(self) -> None:
        payload = canonical_patient_payload(
            {
                "code": "SUCCESS",
                "data": [
                    {"patient_id": "P1", "exam_item": "A", "extra": "drop"},
                    {"patient_id": "P1", "examItemName": "B"},
                ],
                "msg": "成功",
                "success": True,
            }
        )

        self.assertEqual(payload["code"], "SUCCESS")
        self.assertTrue(payload["success"])
        self.assertEqual(len(payload["data"]), 2)
        self.assertEqual(tuple(payload["data"][0]), PATIENT_RESPONSE_FIELDS)
        self.assertEqual(payload["data"][0]["exam_item"], "A")
        self.assertEqual(payload["data"][1]["exam_item"], "B")
        self.assertIsNone(payload["data"][0]["birthday"])
        self.assertNotIn("extra", payload["data"][0])

    def test_canonical_payload_keeps_empty_success_result(self) -> None:
        payload = canonical_patient_payload(
            {"code": "SUCCESS", "data": [], "msg": "成功", "success": True}
        )

        self.assertEqual(payload["data"], [])
        self.assertTrue(payload["success"])


class PatientQueryRouteTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        config = SimpleNamespace(patient_api=patient_config(self.temp.name))
        self.local_api = LocalApi(config, SimpleNamespace(), SimpleNamespace())
        self.original_client = api_module.PatientApiClient
        api_module.PatientApiClient = FakePatientClient
        FakePatientClient.calls = []
        FakePatientClient.error = None
        FakePatientClient.response = {
            "code": "SUCCESS",
            "data": [{field: None for field in PATIENT_RESPONSE_FIELDS}],
            "msg": "成功",
            "success": True,
        }

    def tearDown(self) -> None:
        api_module.PatientApiClient = self.original_client
        self.temp.cleanup()

    def response_json(self, response):
        return json.loads(response.text)

    def test_pure_query_returns_patient_json_without_queue_or_workflow(self) -> None:
        queue_calls = []
        self.local_api.queue = SimpleNamespace(put=lambda event: queue_calls.append(event))
        workflow_calls = []
        self.local_api.workflow = SimpleNamespace(start_scan=lambda code: workflow_calls.append(code))

        response = asyncio.run(
            self.local_api.patient_query(FakeRequest({"code": "60019825336"}))
        )

        self.assertEqual(response.status, 200)
        self.assertTrue(self.response_json(response)["success"])
        self.assertEqual(FakePatientClient.calls, [("60019825336", False)])
        self.assertEqual(queue_calls, [])
        self.assertEqual(workflow_calls, [])

    def test_rejects_non_loopback_and_invalid_codes(self) -> None:
        forbidden = asyncio.run(
            self.local_api.patient_query(
                FakeRequest({"code": "60019825336"}, remote="192.0.2.20")
            )
        )
        invalid = asyncio.run(self.local_api.patient_query(FakeRequest({"code": "患者 1"})))

        self.assertEqual(forbidden.status, 403)
        self.assertEqual(invalid.status, 400)
        self.assertEqual(FakePatientClient.calls, [])

    def test_returns_stable_failure_envelope_for_upstream_error(self) -> None:
        FakePatientClient.error = PatientApiError("private upstream detail")

        response = asyncio.run(
            self.local_api.patient_query(FakeRequest({"code": "60019825336"}))
        )
        payload = self.response_json(response)

        self.assertEqual(response.status, 502)
        self.assertEqual(payload, {
            "code": "FAIL",
            "data": [],
            "msg": "患者查询失败",
            "success": False,
        })


if __name__ == "__main__":
    unittest.main()
