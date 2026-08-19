import importlib.util
import json
import os
import tempfile
import threading
import time
import unittest
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen


MODULE_PATH = Path(__file__).resolve().parents[1] / "camera_ocr_overlay.py"
SPEC = importlib.util.spec_from_file_location("camera_ocr_overlay", MODULE_PATH)
MODULE = importlib.util.module_from_spec(SPEC)
assert SPEC.loader is not None
SPEC.loader.exec_module(MODULE)


def patient_payload(record_count=1):
    records = []
    for index in range(record_count):
        record = {field: None for field in MODULE.PATIENT_RESPONSE_FIELDS}
        record.update(
            {
                "patient_id": "P%d" % (index + 1),
                "patient_name": "测试患者%d" % (index + 1),
                "exam_item": "检查%d" % (index + 1),
            }
        )
        records.append(record)
    return {"code": "SUCCESS", "data": records, "msg": "成功", "success": True}


class NormalizeTriggerStatusTests(unittest.TestCase):
    def test_keeps_geometry_and_capture_progress(self):
        status = MODULE.normalize_trigger_status(
            {
                "frame_size": {"width": 100, "height": 50},
                "paper_detected": True,
                "paper_confidence": 0.94,
                "paper_corners": [[-3, 2], [110, 2], [99, 60], [1, 48]],
                "capture_stage": "collecting_a",
                "burst": {"ready": False, "collected_frames": 2, "target_frames": 3},
            }
        )

        self.assertTrue(status["paper_detected"])
        self.assertEqual(status["paper_corners"], [[0.0, 2.0], [100.0, 2.0], [99.0, 50.0], [1.0, 48.0]])
        self.assertEqual(status["capture_stage"], "collecting_a")
        self.assertEqual(status["burst"]["collected_frames"], 2)

    def test_drops_patient_values_and_invalid_geometry(self):
        status = MODULE.normalize_trigger_status(
            {
                "paper_detected": True,
                "paper_corners": [[1, 2], [3, 4]],
                "identifier": "03D2026072802066",
                "field_a": {"value": "03D2026072802066"},
            }
        )

        self.assertFalse(status["paper_detected"])
        self.assertEqual(status["paper_corners"], [])
        self.assertNotIn("identifier", json.dumps(status))
        self.assertNotIn("03D2026072802066", json.dumps(status))


class TriggerStatusCacheTests(unittest.TestCase):
    def test_reads_status_and_marks_stale_file_inactive(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera-trigger.json"
            path.write_text(
                json.dumps(
                    {
                        "paper_detected": True,
                        "paper_corners": [[1, 1], [99, 1], [99, 49], [1, 49]],
                        "frame_size": {"width": 100, "height": 50},
                    }
                ),
                encoding="utf-8",
            )
            cache = MODULE.TriggerStatusCache(path, stale_seconds=0.5)
            fresh = cache.snapshot()
            self.assertTrue(fresh["active"])
            self.assertEqual(fresh["generation"], 1)

            old = time.time() - 10
            os.utime(path, (old, old))
            stale = cache.snapshot()
            self.assertFalse(stale["active"])
            self.assertEqual(stale["service_state"], "busy")

    def test_missing_file_is_a_clean_inactive_state(self):
        with tempfile.TemporaryDirectory() as directory:
            cache = MODULE.TriggerStatusCache(Path(directory) / "missing.json", stale_seconds=1)
            status = cache.snapshot()
            self.assertTrue(status["ok"])
            self.assertFalse(status["active"])
            self.assertEqual(status["service_state"], "waiting")

    def test_marks_short_status_pause_as_busy_instead_of_offline(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "camera-trigger.json"
            path.write_text(json.dumps({"capture_stage": "collecting_a"}), encoding="utf-8")
            old = time.time() - 5
            os.utime(path, (old, old))

            status = MODULE.TriggerStatusCache(
                path,
                stale_seconds=3,
                offline_seconds=20,
            ).snapshot()

            self.assertFalse(status["active"])
            self.assertEqual(status["service_state"], "busy")


class VerifiedResultStoreTests(unittest.TestCase):
    def _write_rules(self, path: Path) -> None:
        path.write_text(
            json.dumps(
                {
                    "enabled": True,
                    "profile": "single-length-16",
                    "fields": [
                        {
                            "type": "selected_identifier",
                            "lengths": [16],
                            "charset": "alphanumeric",
                            "allow_unlabeled": True,
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    def _write_full_text(self, path: Path, capture_id: str) -> None:
        path.write_text(
            json.dumps(
                {
                    "status": "accepted",
                    "capture_id": capture_id,
                    "source": {
                        "frame_size": {"width": 1920, "height": 1080},
                        "paper_corners": [[200, 100], [1700, 120], [1680, 980], [220, 960]],
                        "ocr_rotation": 180,
                    },
                    "document": {
                        "image_size": [1000, 700],
                        "full_text": "untrusted duplicate",
                        "blocks": [
                            {
                                "id": 2,
                                "line_id": 1,
                                "text": "张三",
                                "normalized_box": [200, 100, 300, 160],
                                "score": 0.96,
                            },
                            {
                                "id": 1,
                                "line_id": 1,
                                "text": "姓名",
                                "normalized_box": [50, 100, 140, 160],
                                "score": 0.98,
                            },
                            {
                                "id": 3,
                                "line_id": 2,
                                "text": "检查项目 腹部超声",
                                "normalized_box": [50, 220, 500, 280],
                                "score": 0.94,
                            },
                        ],
                    },
                },
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )

    def test_only_returns_identifier_for_live_verified_status(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / "rules.json"
            result = root / "result.json"
            self._write_rules(rules)
            result.write_text(
                json.dumps({"status": "accepted", "identifier": "03D2026072802066"}),
                encoding="utf-8",
            )
            store = MODULE.VerifiedResultStore(result, rules)

            waiting = store.snapshot({"active": True, "capture_stage": "tracking"})
            self.assertFalse(waiting["available"])
            self.assertNotIn("03D2026072802066", json.dumps(waiting))

            accepted = store.snapshot(
                {
                    "active": True,
                    "capture_stage": "verified",
                    "verification": {"status": "accepted", "reason": "exact_match", "attempt": 1},
                }
            )
            self.assertTrue(accepted["available"])
            self.assertEqual(accepted["identifier"], "03D2026072802066")

    def test_rejects_result_that_does_not_match_active_rule(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / "rules.json"
            result = root / "result.json"
            self._write_rules(rules)
            result.write_text(
                json.dumps({"status": "accepted", "identifier": "60016373728"}),
                encoding="utf-8",
            )
            store = MODULE.VerifiedResultStore(result, rules)
            response = store.snapshot(
                {
                    "active": True,
                    "capture_stage": "verified",
                    "verification": {"status": "accepted"},
                }
            )
            self.assertFalse(response["available"])
            self.assertNotIn("60016373728", json.dumps(response))

    def test_returns_all_ocr_text_only_for_the_matching_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / "rules.json"
            result = root / "result.json"
            full_text = root / "full-text.json"
            self._write_rules(rules)
            result.write_text(
                json.dumps(
                    {
                        "status": "accepted",
                        "identifier": "03D2026072802066",
                        "capture_id": "capture-a",
                    }
                ),
                encoding="utf-8",
            )
            self._write_full_text(full_text, "capture-a")
            store = MODULE.VerifiedResultStore(result, rules, full_text)
            live = {
                "active": True,
                "capture_stage": "verified",
                "capture_id": "capture-a",
                "verification": {"status": "accepted", "reason": "exact_match", "attempt": 1},
            }

            accepted = store.snapshot(live)

            self.assertTrue(accepted["document"]["available"])
            self.assertEqual(accepted["document"]["full_text"], "姓名 张三\n检查项目 腹部超声")
            self.assertEqual(accepted["document"]["item_count"], 3)
            self.assertEqual(accepted["document"]["source"]["ocr_rotation"], 180)

            self._write_full_text(full_text, "capture-b")
            stale = store.snapshot(live)
            self.assertFalse(stale["document"]["available"])
            self.assertEqual(stale["document"]["status"], "stale")
            self.assertNotIn("张三", json.dumps(stale, ensure_ascii=False))

    def test_returns_full_text_when_identifier_verification_is_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / "rules.json"
            result = root / "result.json"
            full_text = root / "full-text.json"
            self._write_rules(rules)
            self._write_full_text(full_text, "capture-a")
            store = MODULE.VerifiedResultStore(result, rules, full_text)

            response = store.snapshot(
                {
                    "active": True,
                    "capture_stage": "verification_rejected",
                    "capture_id": "capture-a",
                    "verification": {"status": "rejected", "reason": "field_a_rejected", "attempt": 3},
                }
            )

            self.assertTrue(response["available"])
            self.assertFalse(response["identifier_available"])
            self.assertTrue(response["document"]["available"])
            self.assertIn("检查项目", response["document"]["full_text"])

    def test_page_contains_full_text_panel_and_camera_box_mapping(self):
        page = MODULE.PAGE.decode("utf-8")
        self.assertIn('id="ocrText"', page)
        self.assertIn("drawOcrBlocks", page)
        self.assertIn("navigator.clipboard.writeText", page)
        self.assertIn('id="patientQueryEnabled"', page)
        self.assertIn('id="autoEntryEnabled"', page)
        self.assertIn('id="exportPatient"', page)


class VerifiedPatientResultStoreTests(unittest.TestCase):
    def test_keeps_exact_envelope_all_records_and_rejects_stale_capture(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MODULE.VerifiedPatientResultStore(
                root / "verified-patient.json",
                root / "verified-patient.meta.json",
            )
            source = patient_payload(2)
            source["data"][0]["unexpected"] = "drop"

            canonical = store.write("a" * 32, "a" * 64, source, created_at=10.0)
            status, current = store.snapshot({"active": True, "capture_id": "a" * 32})
            stale_status, stale = store.snapshot({"active": True, "capture_id": "b" * 32})

            self.assertEqual(status, 200)
            self.assertEqual(current, canonical)
            self.assertEqual(len(current["data"]), 2)
            self.assertEqual(tuple(current["data"][0]), MODULE.PATIENT_RESPONSE_FIELDS)
            self.assertNotIn("unexpected", current["data"][0])
            self.assertEqual(stale_status, 202)
            self.assertEqual(stale["code"], "PENDING")
            self.assertNotIn("测试患者", json.dumps(stale, ensure_ascii=False))
            self.assertNotIn(
                "capture_id",
                json.loads((root / "verified-patient.json").read_text(encoding="utf-8")),
            )
            if os.name != "nt":
                self.assertEqual((root / "verified-patient.json").stat().st_mode & 0o777, 0o600)
                self.assertEqual((root / "verified-patient.meta.json").stat().st_mode & 0o777, 0o600)


class CaptureConfigurationStoreTests(unittest.TestCase):
    def test_persists_rotations_rule_and_trigger_environment(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / "rules.json"
            rules.write_text(
                json.dumps(
                    {
                        "profile": "single-length-16",
                        "fields": [{"lengths": [16], "charset": "alphanumeric"}],
                    }
                ),
                encoding="utf-8",
            )
            restarts = []
            store = MODULE.CaptureConfigurationStore(
                root / "capture.json",
                rules,
                root / "capture.env",
                restart_trigger=lambda: restarts.append(True),
            )

            saved = store.update(
                {
                    "display_rotation": 270,
                    "ocr_rotation": 180,
                    "match": {"length": 11, "charset": "digits"},
                    "forward_to_gateway": True,
                }
            )

            self.assertEqual(saved["display_rotation"], 270)
            self.assertEqual(saved["ocr_rotation"], 180)
            self.assertEqual(saved["match"], {"length": 11, "charset": "digits"})
            self.assertTrue(saved["forward_to_gateway"])
            self.assertGreater(saved["forwarding_enabled_at"], 0)
            self.assertEqual(restarts, [True])
            self.assertEqual((root / "capture.env").read_text(encoding="utf-8"), "OCR_ROTATION=270\n")
            persisted_capture = json.loads((root / "capture.json").read_text(encoding="utf-8"))
            self.assertEqual(persisted_capture["version"], 3)
            self.assertEqual(persisted_capture["rotation_reference"], "current_orientation_zero")
            self.assertTrue(persisted_capture["forward_to_gateway"])
            self.assertTrue(persisted_capture["patient_query_enabled"])
            self.assertTrue(persisted_capture["auto_entry_enabled"])
            persisted_rules = json.loads(rules.read_text(encoding="utf-8"))
            self.assertEqual(persisted_rules["profile"], "single-length-11")
            self.assertEqual(persisted_rules["fields"][0]["lengths"], [11])

    def test_forward_toggle_alone_does_not_restart_detector(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            rules = root / "rules.json"
            rules.write_text(
                json.dumps(
                    {
                        "profile": "single-length-11",
                        "fields": [{"lengths": [11], "charset": "digits"}],
                    }
                ),
                encoding="utf-8",
            )
            restarts = []
            store = MODULE.CaptureConfigurationStore(
                root / "capture.json",
                rules,
                root / "capture.env",
                restart_trigger=lambda: restarts.append(True),
            )

            saved = store.update(
                {
                    "display_rotation": 0,
                    "ocr_rotation": 0,
                    "match": {"length": 11, "charset": "digits"},
                    "forward_to_gateway": True,
                }
            )

            self.assertTrue(saved["forward_to_gateway"])
            self.assertEqual(restarts, [])

    def test_two_action_switches_are_independent_and_v2_auto_entry_migrates(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            settings = root / "capture.json"
            settings.write_text(
                json.dumps(
                    {
                        "version": 2,
                        "display_rotation": 0,
                        "ocr_rotation": 0,
                        "forward_to_gateway": True,
                        "forwarding_enabled_at": 12.0,
                        "updated_at": 11.0,
                    }
                ),
                encoding="utf-8",
            )
            store = MODULE.CaptureConfigurationStore(
                settings,
                root / "rules.json",
                root / "capture.env",
            )

            migrated = store.snapshot()
            self.assertTrue(migrated["patient_query_enabled"])
            self.assertEqual(migrated["patient_query_enabled_at"], 11.0)
            self.assertTrue(migrated["auto_entry_enabled"])
            self.assertEqual(migrated["auto_entry_enabled_at"], 12.0)

            updated = store.update(
                {
                    "display_rotation": 0,
                    "ocr_rotation": 0,
                    "match": {"length": 11, "charset": "digits"},
                    "patient_query_enabled": False,
                    "auto_entry_enabled": True,
                }
            )
            self.assertFalse(updated["patient_query_enabled"])
            self.assertTrue(updated["auto_entry_enabled"])

    def test_rejects_unsupported_values_without_writing(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MODULE.CaptureConfigurationStore(
                root / "capture.json",
                root / "rules.json",
                root / "capture.env",
            )
            with self.assertRaisesRegex(ValueError, "display_rotation"):
                store.update(
                    {
                        "display_rotation": 45,
                        "ocr_rotation": 90,
                        "match": {"length": 16, "charset": "alphanumeric"},
                    }
                )
            self.assertFalse((root / "capture.json").exists())

    def test_migrates_previous_absolute_90_degree_settings_to_zero(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "capture.json").write_text(
                json.dumps(
                    {
                        "version": 1,
                        "display_rotation": 90,
                        "ocr_rotation": 90,
                    }
                ),
                encoding="utf-8",
            )
            store = MODULE.CaptureConfigurationStore(
                root / "capture.json",
                root / "rules.json",
                root / "capture.env",
            )
            config = store.snapshot()
            self.assertEqual(config["display_rotation"], 0)
            self.assertEqual(config["ocr_rotation"], 0)


class MonitorHttpTests(unittest.TestCase):
    def test_result_is_live_and_status_does_not_duplicate_identifier(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_path = root / "status.json"
            rules_path = root / "rules.json"
            result_path = root / "result.json"
            status_path.write_text(
                json.dumps(
                    {
                        "capture_stage": "verified",
                        "verification": {"status": "accepted", "reason": "exact_match", "attempt": 1},
                    }
                ),
                encoding="utf-8",
            )
            rules_path.write_text(
                json.dumps(
                    {
                        "profile": "single-length-16",
                        "fields": [{"lengths": [16], "charset": "alphanumeric", "allow_unlabeled": True}],
                    }
                ),
                encoding="utf-8",
            )
            result_path.write_text(
                json.dumps({"status": "accepted", "identifier": "03D2026072802066"}),
                encoding="utf-8",
            )
            cache = MODULE.TriggerStatusCache(status_path, stale_seconds=3)
            store = MODULE.VerifiedResultStore(result_path, rules_path)
            config_store = MODULE.CaptureConfigurationStore(
                root / "capture.json",
                rules_path,
                root / "capture.env",
            )
            server = MODULE.Server(("127.0.0.1", 0), cache, store, config_store)
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:%d" % server.server_address[1]
            try:
                public = json.loads(urlopen(base + "/api/status", timeout=2).read())
                self.assertNotIn("03D2026072802066", json.dumps(public))
                self.assertEqual(public["rule"]["fields"][0]["lengths"], [16])

                private = json.loads(urlopen(base + "/api/result", timeout=2).read())
                self.assertEqual(private["identifier"], "03D2026072802066")

                request = Request(
                    base + "/api/config",
                    data=json.dumps(
                        {
                            "display_rotation": 180,
                            "ocr_rotation": 270,
                            "match": {"length": 10, "charset": "digits"},
                            "forward_to_gateway": True,
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                updated = json.loads(urlopen(request, timeout=2).read())
                self.assertTrue(updated["saved"])
                self.assertEqual(updated["config"]["display_rotation"], 180)
                self.assertEqual(updated["config"]["ocr_rotation"], 270)
                self.assertEqual(updated["config"]["match"]["length"], 10)
                self.assertTrue(updated["config"]["forward_to_gateway"])

                bad_request = Request(
                    base + "/api/config",
                    data=json.dumps(
                        {
                            "display_rotation": 45,
                            "ocr_rotation": 90,
                            "match": {"length": 10, "charset": "digits"},
                            "forward_to_gateway": True,
                        }
                    ).encode("utf-8"),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with self.assertRaises(HTTPError) as invalid:
                    urlopen(bad_request, timeout=2)
                self.assertEqual(invalid.exception.code, 400)
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_patient_endpoint_returns_only_matching_capture_payload(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status_path = root / "status.json"
            rules_path = root / "rules.json"
            result_path = root / "result.json"
            status_path.write_text(
                json.dumps(
                    {
                        "capture_stage": "verified",
                        "capture_id": "a" * 32,
                        "verification": {"status": "accepted"},
                    }
                ),
                encoding="utf-8",
            )
            rules_path.write_text(
                json.dumps(
                    {
                        "profile": "single-length-11",
                        "fields": [{"lengths": [11], "charset": "digits"}],
                    }
                ),
                encoding="utf-8",
            )
            result_path.write_text(
                json.dumps(
                    {
                        "status": "accepted",
                        "identifier": "60016373728",
                        "capture_id": "a" * 32,
                        "created_at": 10.0,
                    }
                ),
                encoding="utf-8",
            )
            cache = MODULE.TriggerStatusCache(status_path, stale_seconds=30)
            result_store = MODULE.VerifiedResultStore(result_path, rules_path)
            config_store = MODULE.CaptureConfigurationStore(
                root / "capture.json", rules_path, root / "capture.env"
            )
            patient_store = MODULE.VerifiedPatientResultStore(
                root / "patient.json", root / "patient-meta.json"
            )
            patient_store.write("a" * 32, "a" * 64, patient_payload(2))
            server = MODULE.Server(
                ("127.0.0.1", 0),
                cache,
                result_store,
                config_store,
                patient_store=patient_store,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            base = "http://127.0.0.1:%d" % server.server_address[1]
            try:
                payload = json.loads(urlopen(base + "/api/patient", timeout=2).read())
                self.assertTrue(payload["success"])
                self.assertEqual(len(payload["data"]), 2)

                status_path.write_text(
                    json.dumps(
                        {
                            "capture_stage": "verified",
                            "capture_id": "b" * 32,
                            "verification": {"status": "accepted"},
                        }
                    ),
                    encoding="utf-8",
                )
                stamp = time.time_ns() + 1_000_000
                os.utime(status_path, ns=(stamp, stamp))
                cache.last_mtime_ns = -1
                self.assertEqual(cache.snapshot()["capture_id"], "b" * 32)
                with urlopen(base + "/api/patient", timeout=2) as pending:
                    self.assertEqual(pending.status, 202)
                    pending_payload = json.loads(pending.read())
                self.assertEqual(pending_payload["code"], "PENDING")
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)


class VerifiedPatientQueryTests(unittest.TestCase):
    def _build(self, root: Path, sent):
        rules = root / "rules.json"
        status = root / "status.json"
        result = root / "result.json"
        rules.write_text(
            json.dumps(
                {
                    "profile": "single-length-11",
                    "fields": [{"lengths": [11], "charset": "digits"}],
                }
            ),
            encoding="utf-8",
        )
        status.write_text(
            json.dumps({"capture_stage": "absent", "paper_detected": False}),
            encoding="utf-8",
        )
        cache = MODULE.TriggerStatusCache(status, stale_seconds=30)
        result_store = MODULE.VerifiedResultStore(result, rules)
        config_store = MODULE.CaptureConfigurationStore(
            root / "capture.json", rules, root / "capture.env"
        )
        config_store.update(
            {
                "display_rotation": 0,
                "ocr_rotation": 0,
                "match": {"length": 11, "charset": "digits"},
                "patient_query_enabled": False,
                "auto_entry_enabled": False,
            }
        )
        patient_store = MODULE.VerifiedPatientResultStore(
            root / "patient.json", root / "patient-meta.json"
        )

        def sender(endpoint, identifier, timeout_seconds):
            sent.append((endpoint, identifier, timeout_seconds))
            return patient_payload(2)

        query = MODULE.VerifiedPatientQuery(
            cache,
            result_store,
            config_store,
            patient_store,
            root / "patient-query-state.json",
            "http://127.0.0.1:8080/patient/query",
            sender=sender,
        )
        return status, result, config_store, patient_store, query

    def _write_status(self, path: Path, stage: str, capture_id="a" * 32) -> None:
        payload = {
            "capture_stage": stage,
            "paper_detected": stage != "absent",
            "capture_id": capture_id if stage != "absent" else None,
        }
        if stage == "verified":
            payload["verification"] = {"status": "accepted"}
        path.write_text(json.dumps(payload), encoding="utf-8")
        stamp = time.time_ns() + 1_000_000
        os.utime(path, ns=(stamp, stamp))

    def test_queries_once_writes_all_records_and_reuses_persisted_state(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sent = []
            status, result, config_store, patient_store, query = self._build(root, sent)
            enabled = config_store.update(
                {
                    "display_rotation": 0,
                    "ocr_rotation": 0,
                    "match": {"length": 11, "charset": "digits"},
                    "patient_query_enabled": True,
                    "auto_entry_enabled": False,
                }
            )["patient_query_enabled_at"]
            query.poll_once(now=enabled + 0.1)
            self.assertEqual(query.snapshot()["state"], "waiting")

            self._write_status(status, "verified")
            result.write_text(
                json.dumps(
                    {
                        "status": "accepted",
                        "identifier": "60016373728",
                        "capture_id": "a" * 32,
                        "created_at": enabled + 1,
                    }
                ),
                encoding="utf-8",
            )
            query.poll_once(now=enabled + 2)
            query.poll_once(now=enabled + 3)

            self.assertEqual(len(sent), 1)
            self.assertEqual(query.snapshot()["record_count"], 2)
            http_status, payload = patient_store.snapshot(
                {"active": True, "capture_id": "a" * 32}
            )
            self.assertEqual(http_status, 200)
            self.assertEqual(len(payload["data"]), 2)

            restarted = MODULE.VerifiedPatientQuery(
                query.cache,
                query.result_store,
                config_store,
                patient_store,
                root / "patient-query-state.json",
                "http://127.0.0.1:8080/patient/query",
                sender=lambda *args: sent.append(args) or patient_payload(1),
            )
            restarted.poll_once(now=enabled + 4)
            self.assertEqual(len(sent), 1)
            self.assertEqual(restarted.snapshot()["record_count"], 2)

    def test_failure_writes_stable_json_and_schedules_retry(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            status, result, config_store, patient_store, query = self._build(root, [])
            enabled = config_store.update(
                {
                    "display_rotation": 0,
                    "ocr_rotation": 0,
                    "match": {"length": 11, "charset": "digits"},
                    "patient_query_enabled": True,
                    "auto_entry_enabled": False,
                }
            )["patient_query_enabled_at"]
            query.poll_once(now=enabled + 0.1)
            self._write_status(status, "verified")
            result.write_text(
                json.dumps(
                    {
                        "status": "accepted",
                        "identifier": "60016373728",
                        "capture_id": "a" * 32,
                        "created_at": enabled + 1,
                    }
                ),
                encoding="utf-8",
            )
            query.sender = lambda *args: (_ for _ in ()).throw(
                MODULE.PatientQueryRequestError(
                    "failed",
                    {"code": "FAIL", "data": [], "msg": "患者查询失败", "success": False},
                )
            )

            query.poll_once(now=enabled + 2)

            self.assertEqual(query.snapshot()["state"], "error")
            http_status, payload = patient_store.snapshot(
                {"active": True, "capture_id": "a" * 32}
            )
            self.assertEqual(http_status, 502)
            self.assertEqual(payload["code"], "FAIL")

    def test_pending_result_uses_http_202(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            store = MODULE.VerifiedPatientResultStore(
                root / "patient.json", root / "patient-meta.json"
            )
            store.write(
                "a" * 32,
                "a" * 64,
                {"code": "PENDING", "data": [], "msg": "患者信息查询中", "success": False},
            )

            status, payload = store.snapshot({"active": True, "capture_id": "a" * 32})

            self.assertEqual(status, 202)
            self.assertEqual(payload["code"], "PENDING")

    def test_all_four_query_and_auto_entry_switch_combinations(self):
        combinations = (
            (False, False, 0, 0),
            (True, False, 1, 0),
            (False, True, 0, 1),
            (True, True, 1, 1),
        )
        with tempfile.TemporaryDirectory() as directory:
            parent = Path(directory)
            for index, (query_enabled, auto_enabled, query_count, auto_count) in enumerate(combinations):
                with self.subTest(query=query_enabled, auto=auto_enabled):
                    root = parent / str(index)
                    root.mkdir()
                    patient_calls = []
                    auto_calls = []
                    status, result, config_store, _, query = self._build(root, patient_calls)
                    auto_entry = MODULE.VerifiedIdentifierForwarder(
                        query.cache,
                        query.result_store,
                        config_store,
                        root / "auto-entry-state.json",
                        "http://127.0.0.1:8080/scan",
                        sender=lambda endpoint, identifier, timeout: auto_calls.append(identifier),
                    )
                    config = config_store.update(
                        {
                            "display_rotation": 0,
                            "ocr_rotation": 0,
                            "match": {"length": 11, "charset": "digits"},
                            "patient_query_enabled": query_enabled,
                            "auto_entry_enabled": auto_enabled,
                        }
                    )
                    enabled_at = max(
                        float(config["patient_query_enabled_at"] or 0),
                        float(config["auto_entry_enabled_at"] or 0),
                        time.time(),
                    )
                    query.poll_once(now=enabled_at + 0.1)
                    auto_entry.poll_once(now=enabled_at + 0.1)

                    capture_id = "%032x" % (index + 1)
                    self._write_status(status, "verified", capture_id=capture_id)
                    result.write_text(
                        json.dumps(
                            {
                                "status": "accepted",
                                "identifier": "60016373728",
                                "capture_id": capture_id,
                                "created_at": enabled_at + 1,
                            }
                        ),
                        encoding="utf-8",
                    )
                    query.poll_once(now=enabled_at + 2)
                    auto_entry.poll_once(now=enabled_at + 2)

                    self.assertEqual(len(patient_calls), query_count)
                    self.assertEqual(len(auto_calls), auto_count)


class VerifiedIdentifierForwarderTests(unittest.TestCase):
    def _build(self, root: Path, sent):
        rules = root / "rules.json"
        status = root / "status.json"
        result = root / "result.json"
        rules.write_text(
            json.dumps(
                {
                    "profile": "single-length-11",
                    "fields": [{"lengths": [11], "charset": "digits"}],
                }
            ),
            encoding="utf-8",
        )
        status.write_text(
            json.dumps(
                {
                    "capture_stage": "verified",
                    "verification": {"status": "accepted"},
                }
            ),
            encoding="utf-8",
        )
        cache = MODULE.TriggerStatusCache(status, stale_seconds=30)
        result_store = MODULE.VerifiedResultStore(result, rules)
        config_store = MODULE.CaptureConfigurationStore(
            root / "capture.json",
            rules,
            root / "capture.env",
        )

        def sender(endpoint, identifier, timeout_seconds):
            sent.append((endpoint, identifier, timeout_seconds))

        forwarder = MODULE.VerifiedIdentifierForwarder(
            cache,
            result_store,
            config_store,
            root / "forward-state.json",
            "http://127.0.0.1:8080/scan",
            sender=sender,
        )
        return status, result, config_store, forwarder

    def _write_status(self, path: Path, stage: str) -> None:
        payload = {"capture_stage": stage, "paper_detected": stage != "absent"}
        if stage == "verified":
            payload["verification"] = {"status": "accepted"}
        path.write_text(json.dumps(payload), encoding="utf-8")
        stamp = time.time_ns() + 1_000_000
        os.utime(path, ns=(stamp, stamp))

    def test_only_forwards_new_verified_result_once(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sent = []
            status, result, config_store, forwarder = self._build(root, sent)
            result.write_text(
                json.dumps(
                    {"status": "accepted", "identifier": "60016373728", "created_at": 10.0}
                ),
                encoding="utf-8",
            )

            forwarder.poll_once(now=20.0)
            self.assertEqual(forwarder.snapshot()["state"], "disabled")
            self.assertEqual(sent, [])

            config_store.update(
                {
                    "display_rotation": 0,
                    "ocr_rotation": 0,
                    "match": {"length": 11, "charset": "digits"},
                    "forward_to_gateway": True,
                }
            )
            enabled_at = config_store.snapshot()["forwarding_enabled_at"]
            forwarder.poll_once(now=enabled_at + 1)
            self.assertEqual(forwarder.snapshot()["state"], "clear_required")
            self.assertEqual(sent, [])

            self._write_status(status, "absent")
            forwarder.poll_once(now=enabled_at + 1.1)
            self.assertEqual(forwarder.snapshot()["state"], "waiting")

            self._write_status(status, "verified")
            result.write_text(
                json.dumps(
                    {
                        "status": "accepted",
                        "identifier": "60016373728",
                        "created_at": enabled_at + 2,
                    }
                ),
                encoding="utf-8",
            )
            forwarder.poll_once(now=enabled_at + 3)
            forwarder.poll_once(now=enabled_at + 4)
            self.assertEqual(len(sent), 1)
            self.assertEqual(sent[0][1], "60016373728")
            self.assertEqual(forwarder.snapshot()["state"], "sent")

    def test_same_identifier_can_be_forwarded_for_a_new_capture_and_survives_restart(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            sent = []
            status, result, config_store, forwarder = self._build(root, sent)
            config_store.update(
                {
                    "display_rotation": 0,
                    "ocr_rotation": 0,
                    "match": {"length": 11, "charset": "digits"},
                    "forward_to_gateway": True,
                }
            )
            enabled_at = config_store.snapshot()["forwarding_enabled_at"]
            self._write_status(status, "absent")
            forwarder.poll_once(now=enabled_at + 0.1)
            self._write_status(status, "verified")

            for offset in (1.0, 2.0):
                result.write_text(
                    json.dumps(
                        {
                            "status": "accepted",
                            "identifier": "60016373728",
                            "created_at": enabled_at + offset,
                        }
                    ),
                    encoding="utf-8",
                )
                forwarder.poll_once(now=enabled_at + offset + 0.1)
            self.assertEqual(len(sent), 2)

            restarted = MODULE.VerifiedIdentifierForwarder(
                forwarder.cache,
                forwarder.result_store,
                config_store,
                root / "forward-state.json",
                "http://127.0.0.1:8080/scan",
                sender=lambda endpoint, identifier, timeout: sent.append((endpoint, identifier, timeout)),
            )
            restarted.poll_once(now=enabled_at + 4)
            self.assertEqual(len(sent), 2)


class SystemdServiceProbeTests(unittest.TestCase):
    def test_snapshot_caches_service_state(self):
        probe = MODULE.SystemdServiceProbe("example.service", refresh_seconds=10)
        calls = []
        probe._query = lambda: calls.append(True) or "active"

        first = probe.snapshot(now=100)
        second = probe.snapshot(now=101)

        self.assertEqual(first["state"], "active")
        self.assertEqual(second["state"], "active")
        self.assertEqual(calls, [True])


if __name__ == "__main__":
    unittest.main()
