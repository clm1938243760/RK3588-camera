from __future__ import annotations

import copy
import hashlib
import json
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from aiohttp import CookieJar
from aiohttp.test_utils import TestClient, TestServer

from rk3588_gateway.report_center.archive import ReportArchive
from rk3588_gateway.report_center.camera_patient import CameraPatientResolver
from rk3588_gateway.report_center.camera_runtime import (
    build_runtime_template,
    find_configuration_image,
    full_page_once_status,
    request_full_page_once,
    retain_entry_image,
    retain_configuration_image,
    write_runtime_template,
)
from rk3588_gateway.report_center.config import ReportCenterConfig
from rk3588_gateway.report_center.connectors import json_path_get, render_sql_template
from rk3588_gateway.report_center.coordinator import ReportCenterCoordinator
from rk3588_gateway.report_center.domain import ConflictError, ValidationError
from rk3588_gateway.report_center.ocr_fields import RuleFieldResolver
from rk3588_gateway.report_center.store import ReportCenterStore
from rk3588_gateway.report_center.upload import ReportCenterUploadWorker
from rk3588_gateway.report_center.web import ReportCenterWeb, _validate_profile


def profile_config(**updates):
    result = {
        "patient_input_mode": "manual",
        "camera_intake_enabled": False,
        "camera_patient_enabled": False,
        "patient_connector_id": None,
        "exam_item_filter": "",
        "auto_entry_enabled": False,
        "report_source": "msc",
        "field_resolver": {"provider": "rules", "fields": []},
        "hid": {"template_path": "", "actions": []},
        "upload_target_id": None,
    }
    result.update(updates)
    return result


def make_center_config(root: Path) -> ReportCenterConfig:
    data = root / "center"
    return ReportCenterConfig(
        enabled=True,
        shadow_mode=True,
        intake_only=False,
        data_dir=str(data),
        database_path=str(data / "db" / "report-center.sqlite3"),
        archive_dir=str(data / "archive"),
        incoming_dir=str(data / "incoming"),
        host="127.0.0.1",
        port=0,
        ssl_cert="",
        ssl_key="",
        session_hours=1,
        retention_days=90,
        report_info_path=str(data / "ReportInfo.xml"),
        upload_poll_seconds=1,
        upload_retry_seconds=1,
        upload_max_attempts=3,
        camera_loopback_only=True,
        bootstrap_admin_password="",
        portal_dir="",
        camera_configuration_image_dir=str(data / "runtime-images"),
        template_image_dir=str(data / "template-images"),
        camera_template_runtime_file=str(data / "active-camera-template.json"),
        camera_full_page_once_file=str(data / "force-full-page-once"),
        hid_active_marker=str(data / "hid-active"),
        external_report_token_file=str(data / "report-link.token"),
        external_report_window_seconds=7200,
        entry_capture_dir=str(data / "entry-captures"),
    )


def camera_capture_payload(capture_id: str = "capture-001"):
    return {
        "status": "review_required",
        "capture_id": capture_id,
        "created_at": 1787190000.0,
        "source": {
            "selected_frame_sha256": "a" * 64,
            "ocr_rotation": 90,
        },
        "quality": {"selected_frame": {"sharpness": 123.4}},
        "document": {
            "schema_version": 2,
            "image_size": [3200, 2400],
            "full_text": "患者ID 60019825336",
            "lines": [{"id": 1, "text": "患者ID 60019825336"}],
            "blocks": [
                {"id": 1, "line_id": 1, "text": "患者ID", "score": 0.9},
                {"id": 2, "line_id": 1, "text": "60019825336", "score": 0.8},
            ],
        },
    }


class StoreAndArchiveTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = make_center_config(self.root)
        self.store = ReportCenterStore(self.config.database_path)
        self.store.bootstrap_admin("password123")
        profile_id = self.store.create_profile("default", profile_config(), "test")
        self.store.publish_profile(profile_id, "test")
        self.archive = ReportArchive(self.config, self.store)

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_upload_worker_publishes_display_states(self) -> None:
        display_path = self.root / "display-state.json"
        worker = ReportCenterUploadWorker(self.config, self.store, str(display_path))

        worker._publish_uploading()
        uploading = json.loads(display_path.read_text(encoding="utf-8"))
        worker._publish_upload_result(False, "HTTP 503")
        failed = json.loads(display_path.read_text(encoding="utf-8"))

        self.assertEqual(uploading["display"]["screen"], "report_uploading")
        self.assertEqual(failed["display"]["screen"], "report_upload_failed")
        self.assertEqual(failed["display"]["upload_error"], "HTTP 503")
        self.assertIn("expires_at", failed)

    def test_only_queue_head_can_advance_and_missing_releases_next(self) -> None:
        first = self.store.create_session("manual", {"patient_name": "A"})
        second = self.store.create_session("manual", {"patient_name": "B"})
        self.assertEqual(self.store.next_queued_session()["id"], first["id"])
        self.store.transition_session(first["id"], "entering")
        self.store.transition_session(first["id"], "awaiting_report")
        self.assertIsNone(self.store.next_queued_session())
        self.store.transition_session(first["id"], "report_missing")
        self.assertEqual(self.store.next_queued_session()["id"], second["id"])

    def test_intake_only_entry_completion_releases_next_patient(self) -> None:
        first = self.store.create_session("camera", {"patient_name": "A"})
        second = self.store.create_session("camera", {"patient_name": "B"})
        self.store.transition_session(first["id"], "entering")
        self.store.transition_session(first["id"], "entry_completed")
        self.assertEqual(
            self.store.next_queued_session(ignore_report_wait=True)["id"],
            second["id"],
        )

    def test_external_report_links_latest_unbound_entry_and_is_idempotent(self) -> None:
        first = self.store.create_session("camera", {"patient_name": "A"}, capture_id="cap-a")
        second = self.store.create_session("camera", {"patient_name": "B"}, capture_id="cap-b")
        for session in (first, second):
            self.store.transition_session(session["id"], "entering")
            self.store.transition_session(session["id"], "entry_completed")
        created_at = float(self.store.get_session(second["id"])["entered_at"]) + 1
        digest = "a" * 64
        linked = self.store.associate_external_report(1, digest, "printer", created_at, 7200)
        self.assertEqual(linked["status"], "linked")
        self.assertEqual(linked["patient_session_id"], second["id"])
        self.assertEqual(linked["capture_id"], "cap-b")
        duplicate = self.store.associate_external_report(99, digest, "printer", created_at, 7200)
        self.assertEqual(duplicate["patient_session_id"], second["id"])
        self.assertEqual(self.store.get_session(second["id"])["status"], "completed")

    def test_external_report_outside_window_is_unlinked(self) -> None:
        session = self.store.create_session("camera", {"patient_name": "A"})
        self.store.transition_session(session["id"], "entering")
        session = self.store.transition_session(session["id"], "entry_completed")
        result = self.store.associate_external_report(
            1, "b" * 64, "msc", float(session["entered_at"]) + 7201, 7200
        )
        self.assertEqual(result["status"], "unlinked")
        self.assertIsNone(result["patient_session_id"])

    def test_camera_capture_is_immutable_and_idempotent(self) -> None:
        payload = camera_capture_payload()
        captured, created = self.store.record_camera_capture(payload)
        self.assertTrue(created)
        self.assertEqual(captured["block_count"], 2)
        self.assertAlmostEqual(captured["average_confidence"], 0.85)
        duplicate, created = self.store.record_camera_capture(copy.deepcopy(payload))
        self.assertFalse(created)
        self.assertEqual(duplicate["payload_sha256"], captured["payload_sha256"])
        changed = copy.deepcopy(payload)
        changed["document"]["blocks"][1]["text"] = "different"
        with self.assertRaises(ConflictError):
            self.store.record_camera_capture(changed)

    def test_camera_capture_list_does_not_expose_ocr_text(self) -> None:
        self.store.record_camera_capture(camera_capture_payload())
        summaries = self.store.list_camera_captures()
        self.assertEqual(len(summaries), 1)
        self.assertNotIn("payload", summaries[0])
        self.assertEqual(self.store.counts()["camera_captures"], {"review_required": 1})

    def test_camera_patient_result_is_versioned_and_idempotent(self) -> None:
        self.store.record_camera_capture(camera_capture_payload())
        result = {
            "status": "accepted",
            "response": {"code": "SUCCESS", "data": [], "msg": "成功", "success": True},
            "evidence": {"patient_id": {"span_ids": [2]}},
            "missing_fields": [],
            "conflict_fields": [],
        }
        saved, created = self.store.record_camera_patient_result(
            "capture-001", 1, {"provider": "rules", "fields": []}, result, "test"
        )
        self.assertTrue(created)
        duplicate, created = self.store.record_camera_patient_result(
            "capture-001", 1, {"provider": "rules", "fields": []}, result, "test"
        )
        self.assertFalse(created)
        self.assertEqual(saved["id"], duplicate["id"])
        self.assertEqual(
            self.store.list_camera_captures()[0]["patient_result_status"], "accepted"
        )

    def test_fixed_roi_runtime_snapshot_and_reference_image_retention(self) -> None:
        profile = {
            "id": 7,
            "config": profile_config(
                camera_patient_enabled=True,
                camera_template={"id": "form-1", "name": "申请单1"},
                field_resolver={
                    "provider": "rules",
                    "fields": [
                        {"field_key": "patient_id", "enabled": True, "match_mode": "fixed_roi", "roi": [100, 200, 400, 260]},
                        {"field_key": "sex", "enabled": True, "match_mode": "label_assisted", "roi": None},
                    ],
                },
            ),
        }
        snapshot = build_runtime_template(profile)
        self.assertTrue(snapshot["enabled"])
        self.assertEqual(snapshot["template_name"], "申请单1")
        self.assertEqual([item["field_key"] for item in snapshot["fields"]], ["patient_id"])
        written = write_runtime_template(self.config.camera_template_runtime_file, profile)
        self.assertEqual(written, snapshot)

        runtime = Path(self.config.camera_configuration_image_dir)
        runtime.mkdir(parents=True)
        (runtime / "capture-001.jpg").write_bytes(b"jpeg-reference")
        retained = retain_configuration_image(
            "capture-001", str(runtime), self.config.template_image_dir
        )
        self.assertEqual(retained.read_bytes(), b"jpeg-reference")
        self.assertEqual(
            find_configuration_image("capture-001", str(runtime), self.config.template_image_dir),
            retained,
        )

    def test_full_page_configuration_capture_marker(self) -> None:
        self.assertFalse(
            full_page_once_status(self.config.camera_full_page_once_file)["armed"]
        )
        result = request_full_page_once(
            self.config.camera_full_page_once_file, "admin"
        )
        self.assertTrue(result["armed"])
        self.assertTrue(
            full_page_once_status(self.config.camera_full_page_once_file)["armed"]
        )

    def test_entry_log_retains_fields_status_and_high_resolution_image(self) -> None:
        image = b"jpeg-entry-image"
        runtime = Path(self.config.camera_configuration_image_dir)
        runtime.mkdir(parents=True)
        (runtime / "capture-entry.jpg").write_bytes(image)
        retained = retain_entry_image(
            "capture-entry",
            self.config.camera_configuration_image_dir,
            self.config.template_image_dir,
            self.config.entry_capture_dir,
            hashlib.sha256(image).hexdigest(),
        )
        session = self.store.create_session(
            "camera", {"patient_name": "A", "patient_id": "P001"}, capture_id="capture-entry"
        )
        log_id = self.store.begin_entry_log(
            session["id"],
            "capture-entry",
            session["patient"],
            {"patient_name": "A", "patient_id": "P001"},
            3,
            image_path=str(retained),
            image_size=len(image),
            image_sha256=hashlib.sha256(image).hexdigest(),
        )
        self.store.finish_entry_log(log_id, True)

        entry = self.store.get_entry_log(log_id)
        self.assertEqual(entry["status"], "completed")
        self.assertEqual(entry["fields"]["patient_id"], "P001")
        self.assertEqual(entry["action_count"], 3)
        self.assertTrue(entry["image_available"])
        self.assertNotIn("image_path", entry)

    def test_busy_session_blocks_profile_publish(self) -> None:
        session = self.store.create_session("manual", {"patient_name": "A"})
        self.store.transition_session(session["id"], "entering")
        profile_id = self.store.create_profile("other", profile_config(report_source="printer"), "test")
        with self.assertRaises(ConflictError):
            self.store.publish_profile(profile_id, "test")

    def test_single_pdf_completes_session_and_preserves_hash(self) -> None:
        session = self.store.create_session("manual", {"patient_name": "张三", "patient_id": "P1"})
        self.store.transition_session(session["id"], "entering")
        session = self.store.transition_session(session["id"], "awaiting_report")
        pdf = self.root / "result.pdf"
        pdf.write_bytes(b"%PDF-1.4\nreport\n%%EOF\n")
        report = self.archive.ingest_pdf(pdf, "msc", session)
        self.assertEqual(report["status"], "archived")
        self.assertEqual(self.store.get_session(session["id"])["status"], "completed")
        self.assertTrue(Path(report["archive_path"]).name.startswith("张三_"))
        self.assertEqual(self.store.find_report_by_sha256(report["sha256"])["id"], report["id"])

    def test_multi_pdf_batch_stays_orphan_and_does_not_upload(self) -> None:
        session = self.store.create_session("manual", {"patient_name": "A"})
        self.store.transition_session(session["id"], "entering")
        session = self.store.transition_session(session["id"], "awaiting_report")
        paths = []
        for index in range(2):
            path = self.root / ("result%d.pdf" % index)
            path.write_bytes(("%%PDF-1.4\n%d\n%%%%EOF\n" % index).encode("ascii"))
            paths.append(path)
        target = {"id": 9, "type": "report_multipart", "config": {"endpoint": "http://example.invalid"}}
        reports = self.archive.ingest_batch(paths, "msc", session, upload_target=target)
        self.assertEqual(len(reports), 2)
        self.assertEqual(self.store.get_session(session["id"])["status"], "awaiting_report")
        self.assertEqual(self.store.counts()["uploads"], {})

    def test_uploaded_correction_requeues_only_for_reupload_target(self) -> None:
        target_id = self.store.create_connector(
            "archive", "report_multipart",
            {"endpoint": "http://example.invalid/upload", "correction_mode": "reupload"},
        )
        target = self.store.get_connector(target_id)
        session = self.store.create_session("manual", {"patient_name": "A"})
        self.store.transition_session(session["id"], "entering")
        session = self.store.transition_session(session["id"], "awaiting_report")
        pdf = self.root / "result.pdf"
        pdf.write_bytes(b"%PDF-1.4\nreport\n%%EOF\n")
        report = self.archive.ingest_pdf(pdf, "msc", session, upload_target=target)
        job = self.store.next_upload_job(3)
        self.store.finish_upload(job["id"], True, 3, 1, 200, "", "ok")
        revised = self.store.revise_report_metadata(
            report["id"], {"notes": "corrected"}, "operator correction", "admin"
        )
        self.assertEqual(revised["upload_status"], "pending")
        self.assertEqual(len(self.store.list_report_revisions(report["id"])), 2)

    def test_database_backup_is_readable(self) -> None:
        destination = self.root / "backup" / "center.sqlite3"
        result = self.store.backup(str(destination))
        self.assertGreater(result["size"], 0)
        backup = ReportCenterStore(str(destination))
        self.assertEqual(len(backup.list_profiles()), 1)

    def test_orphan_report_only_assigns_to_waiting_or_missing_session(self) -> None:
        pdf = self.root / "orphan.pdf"
        pdf.write_bytes(b"%PDF-1.4\nreport\n%%EOF\n")
        report = self.archive.ingest_pdf(pdf, "msc", None)
        queued = self.store.create_session("manual", {"patient_name": "Queued"})
        with self.assertRaises(ConflictError):
            self.store.assign_report(report["id"], queued["id"], "operator")
        missing = self.store.create_session("manual", {"patient_name": "Missing"})
        self.store.transition_session(missing["id"], "entering")
        self.store.transition_session(missing["id"], "awaiting_report")
        self.store.transition_session(missing["id"], "report_missing")
        assigned = self.store.assign_report(report["id"], missing["id"], "operator")
        self.assertEqual(assigned["session_id"], missing["id"])
        self.assertEqual(self.store.get_session(missing["id"])["status"], "completed")


class ConnectorAndOcrTests(unittest.TestCase):
    def test_sql_template_uses_typed_escaped_placeholders(self) -> None:
        sql = render_sql_template("x={{query_literal}} OR y LIKE {{query_like}}", "O'Brien")
        self.assertEqual(sql, "x='O''Brien' OR y LIKE '%O''Brien%'")

    def test_limited_json_path(self) -> None:
        payload = {"data": [{"patient": {"id": "P1"}}]}
        self.assertEqual(json_path_get(payload, "$.data[0].patient.id"), "P1")

    def test_rule_resolver_rebuilds_value_from_ocr_span(self) -> None:
        result = RuleFieldResolver().resolve(
            {
                "document": {
                    "blocks": [
                        {"id": 1, "line_id": 1, "text": "患者ID：60019825336", "score": 0.98, "normalized_box": [10, 10, 300, 60]}
                    ]
                }
            },
            [
                {
                    "field_key": "patient_id",
                    "target": "patient_id",
                    "required": True,
                    "label_aliases": ["患者ID"],
                    "relations": ["same_text"],
                    "char_type": "digits",
                    "lengths": [11],
                    "min_ocr_score": 0.8,
                }
            ],
        )
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["patient"]["patient_id"], "60019825336")
        self.assertEqual(result["evidence"]["patient_id"]["span_ids"], [1])

    def test_empty_rule_evidence_requires_review(self) -> None:
        result = RuleFieldResolver().resolve({"document": {"blocks": []}}, [])
        self.assertEqual(result["status"], "review_required")

    def test_fixed_roi_uses_only_the_assigned_region(self) -> None:
        result = RuleFieldResolver().resolve(
            {
                "document": {
                    "blocks": [
                        {"id": 1, "line_id": 1, "text": "60019825336", "score": 0.99, "normalized_box": [100, 100, 300, 150]},
                        {"id": 2, "line_id": 2, "text": "13800138000", "score": 1.0, "normalized_box": [700, 700, 900, 750]},
                    ]
                }
            },
            [{
                "field_key": "patient_id", "target": "patient_id",
                "match_mode": "fixed_roi", "roi": [80, 80, 350, 180],
                "char_type": "digits", "lengths": [11], "min_ocr_score": 0.8,
            }],
        )
        self.assertEqual(result["patient"]["patient_id"], "60019825336")
        self.assertEqual(result["evidence"]["patient_id"]["relation"], "fixed_roi")

    def test_fixed_roi_prefers_field_specific_runtime_ocr(self) -> None:
        result = RuleFieldResolver().resolve(
            {
                "document": {
                    "blocks": [{
                        "id": 1, "line_id": 1, "text": "周安楠", "score": 0.97,
                        "normalized_box": [0, 0, 10, 10],
                        "recognition_source": "roi:patient_name",
                    }]
                }
            },
            [{
                "field_key": "patient_name", "target": "patient_name",
                "match_mode": "fixed_roi", "roi": [500, 500, 600, 600],
                "char_type": "any", "min_ocr_score": 0.8,
            }],
        )
        self.assertEqual(result["patient"]["patient_name"], "周安楠")

    def test_camera_patient_result_matches_query_response_shape(self) -> None:
        payload = {
            "status": "accepted",
            "document": {
                "blocks": [
                    {"id": 1, "line_id": 1, "text": "姓名：周安楠", "score": 0.98},
                    {"id": 2, "line_id": 2, "text": "出生日期：1989-11-16", "score": 0.97},
                    {"id": 3, "line_id": 3, "text": "患者ID：60019825336", "score": 0.99},
                ]
            },
        }
        fields = [
            {"field_key": "patient_name", "target": "patient_name", "required": True, "label_aliases": ["姓名"]},
            {"field_key": "birthday", "target": "birthday", "required": False, "label_aliases": ["出生日期"]},
            {"field_key": "patient_id", "target": "patient_id", "required": True, "label_aliases": ["患者ID"], "char_type": "digits", "lengths": [11]},
        ]
        result = CameraPatientResolver().resolve(payload, fields)
        self.assertEqual(result["status"], "accepted")
        response = result["response"]
        self.assertEqual(set(response), {"code", "data", "msg", "success"})
        self.assertEqual(response["code"], "SUCCESS")
        self.assertEqual(set(response["data"][0]), {
            "birthday", "exam_item", "ming", "sex", "yue", "his_exam_no", "xing",
            "patient_id", "ri", "patient_name", "name_phonetic", "nian", "report_no", "age",
        })
        self.assertEqual(response["data"][0]["xing"], "周")
        self.assertEqual(response["data"][0]["ming"], "安楠")
        self.assertEqual(response["data"][0]["nian"], "1989")
        self.assertEqual(response["data"][0]["yue"], "11")
        self.assertEqual(response["data"][0]["ri"], "16")

    def test_camera_patient_requires_review_for_missing_required_field(self) -> None:
        result = CameraPatientResolver().resolve(
            {"status": "accepted", "document": {"blocks": []}},
            [{"field_key": "patient_name", "required": True, "label_aliases": ["姓名"]}],
        )
        self.assertEqual(result["status"], "rejected")
        self.assertEqual(result["response"]["data"], [])

    def test_camera_patient_accepts_required_fields_despite_full_text_review(self) -> None:
        payload = camera_capture_payload("capture-required-fields")
        result = CameraPatientResolver().resolve(
            payload,
            [{
                "field_key": "patient_id",
                "target": "patient_id",
                "required": True,
                "label_aliases": [],
                "relations": ["nearest"],
                "char_type": "digits",
                "lengths": [11],
                "min_ocr_score": 0.7,
            }],
        )

        self.assertEqual(payload["status"], "review_required")
        self.assertEqual(result["status"], "accepted")
        self.assertEqual(result["response"]["data"][0]["patient_id"], "60019825336")

    def test_camera_patient_uses_default_fields_when_configuration_is_empty(self) -> None:
        result = CameraPatientResolver().resolve(
            {
                "status": "accepted",
                "document": {
                    "blocks": [
                        {"id": 1, "line_id": 1, "text": "姓名：周安楠", "score": 0.98},
                        {"id": 2, "line_id": 2, "text": "患者ID：60019825336", "score": 0.99},
                    ]
                },
            },
            [],
        )
        self.assertEqual(result["response"]["data"][0]["patient_name"], "周安楠")
        self.assertEqual(result["response"]["data"][0]["patient_id"], "60019825336")
        self.assertEqual(len(result["response"]["data"][0]), 14)

    def test_fixed_label_does_not_match_a_longer_field_name(self) -> None:
        result = RuleFieldResolver().resolve(
            {
                "document": {
                    "blocks": [
                        {"id": 1, "line_id": 1, "text": "姓名拼音：ZHANGSAN", "score": 0.99, "normalized_box": [10, 10, 300, 50]},
                        {"id": 2, "line_id": 2, "text": "姓名：张三", "score": 0.98, "normalized_box": [10, 80, 220, 120]},
                    ]
                }
            },
            [{
                "field_key": "patient_name", "label_aliases": ["姓名"],
                "relations": ["same_text"], "char_type": "any",
                "min_ocr_score": 0.7, "max_distance": 0.18,
            }],
        )

        self.assertEqual(result["patient"]["patient_name"], "张三")
        self.assertEqual(result["evidence"]["patient_name"]["label"], "姓名")

    def test_fixed_label_respects_maximum_right_distance(self) -> None:
        result = RuleFieldResolver().resolve(
            {
                "document": {
                    "blocks": [
                        {"id": 1, "line_id": 1, "text": "卡号", "score": 0.99, "normalized_box": [10, 10, 80, 50]},
                        {"id": 2, "line_id": 1, "text": "60019825336", "score": 0.99, "normalized_box": [400, 10, 580, 50]},
                    ]
                }
            },
            [{
                "field_key": "patient_id", "label_aliases": ["卡号"],
                "relations": ["same_line_right"], "char_type": "digits",
                "lengths": [11], "min_ocr_score": 0.7, "max_distance": 0.18,
            }],
        )

        self.assertEqual(result["evidence"], {})

    def test_fixed_label_allows_small_right_box_overlap(self) -> None:
        result = RuleFieldResolver().resolve(
            {
                "document": {
                    "blocks": [
                        {"id": 1, "line_id": 1, "text": "ID:", "score": 0.6244, "normalized_box": [81, 163, 206, 179]},
                        {"id": 2, "line_id": 1, "text": "60019825336", "score": 0.7102, "normalized_box": [192, 164, 443, 184]},
                    ]
                }
            },
            [{
                "field_key": "patient_id", "label_aliases": ["ID"],
                "relations": ["same_line_right"], "char_type": "digits",
                "lengths": [11], "min_ocr_score": 0.6, "max_distance": 0.18,
            }],
        )

        self.assertEqual(result["patient"]["patient_id"], "60019825336")
        self.assertEqual(result["evidence"]["patient_id"]["relation"], "same_line_right")
        self.assertEqual(result["evidence"]["patient_id"]["span_ids"], [1, 2])

    def test_fixed_label_rejects_excessive_right_box_overlap(self) -> None:
        result = RuleFieldResolver().resolve(
            {
                "document": {
                    "blocks": [
                        {"id": 1, "line_id": 1, "text": "ID:", "score": 0.9, "normalized_box": [80, 160, 210, 180]},
                        {"id": 2, "line_id": 1, "text": "60019825336", "score": 0.9, "normalized_box": [180, 160, 440, 180]},
                    ]
                }
            },
            [{
                "field_key": "patient_id", "label_aliases": ["ID"],
                "relations": ["same_line_right"], "char_type": "digits",
                "lengths": [11], "min_ocr_score": 0.6, "max_distance": 0.18,
            }],
        )

        self.assertEqual(result["evidence"], {})


class FixedHidProfileTests(unittest.TestCase):
    def setUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.store = ReportCenterStore(str(Path(self.temp.name) / "center.sqlite3"))

    def tearDown(self) -> None:
        self.temp.cleanup()

    def test_fixed_absolute_actions_are_validated_without_vision_steps(self) -> None:
        config = profile_config(
            auto_entry_enabled=True,
            hid={
                "coordinate_mode": "fixed_absolute",
                "coordinate_basis": {"width": 1920, "height": 1080},
                "actions": [
                    {"type": "input_field", "field": "patient_id", "x": 682, "y": 362},
                    {"type": "condition", "field": "sex", "equals": "男", "x": 875, "y": 359},
                ],
            },
        )

        checks = _validate_profile(config, self.store)

        self.assertIn("fixed absolute HID actions", checks)

    def test_fixed_absolute_actions_reject_screen_recognition(self) -> None:
        config = profile_config(
            auto_entry_enabled=True,
            hid={
                "coordinate_mode": "fixed_absolute",
                "coordinate_basis": {"width": 1920, "height": 1080},
                "actions": [{"type": "wait_for_text", "text": "新建患者"}],
            },
        )

        with self.assertRaisesRegex(ValidationError, "cannot use screen recognition"):
            _validate_profile(config, self.store)

    def test_fixed_absolute_actions_reject_out_of_bounds_point(self) -> None:
        config = profile_config(
            auto_entry_enabled=True,
            hid={
                "coordinate_mode": "fixed_absolute",
                "coordinate_basis": {"width": 1920, "height": 1080},
                "actions": [{"type": "click", "x": 1920, "y": 500}],
            },
        )

        with self.assertRaisesRegex(ValidationError, "outside the configured screen"):
            _validate_profile(config, self.store)


class EntryWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.config = make_center_config(self.root)
        self.store = ReportCenterStore(self.config.database_path)
        profile_id = self.store.create_profile(
            "entry",
            profile_config(
                auto_entry_enabled=True,
                hid={"actions": [{"type": "wait", "seconds": 0}]},
            ),
            "test",
        )
        self.store.publish_profile(profile_id, "test")
        self.archive = ReportArchive(self.config, self.store)

    async def asyncTearDown(self) -> None:
        self.temp.cleanup()

    def _queued_camera_session(self) -> dict:
        image = b"jpeg-entry-workflow"
        payload = camera_capture_payload("capture-entry-workflow")
        payload["source"]["selected_frame_sha256"] = hashlib.sha256(image).hexdigest()
        self.store.record_camera_capture(payload)
        runtime = Path(self.config.camera_configuration_image_dir)
        runtime.mkdir(parents=True, exist_ok=True)
        (runtime / "capture-entry-workflow.jpg").write_bytes(image)
        return self.store.create_session(
            "camera",
            {"patient_name": "A", "patient_id": "P001"},
            capture_id="capture-entry-workflow",
        )

    async def test_camera_capture_publishes_report_detected_state(self) -> None:
        display_path = self.root / "display-state.json"
        coordinator = ReportCenterCoordinator(
            self.store,
            self.archive,
            SimpleNamespace(),
            shadow_mode=False,
            display_state_path=str(display_path),
        )

        await coordinator.camera_capture(camera_capture_payload("capture-detected"))

        state = json.loads(display_path.read_text(encoding="utf-8"))
        self.assertEqual(state["display"]["screen"], "report_detecting")
        self.assertEqual(state["display"]["capture_id"], "capture-detected")
        self.assertNotIn("expires_at", state)

    async def test_final_required_field_failure_blocks_camera_intake(self) -> None:
        profile_id = self.store.create_profile(
            "blocked-entry",
            profile_config(
                patient_input_mode="camera_direct",
                camera_intake_enabled=True,
                camera_patient_enabled=True,
                auto_entry_enabled=True,
                field_resolver={
                    "provider": "rules",
                    "fields": [{
                        "field_key": "patient_name",
                        "target": "patient_name",
                        "enabled": True,
                        "required": True,
                        "match_mode": "fixed_roi",
                        "roi": [1000, 1000, 1200, 1100],
                        "char_type": "any",
                        "min_ocr_score": 0.95,
                    }],
                },
            ),
            "test",
        )
        self.store.publish_profile(profile_id, "test")
        display_path = self.root / "display-state.json"
        coordinator = ReportCenterCoordinator(
            self.store,
            self.archive,
            SimpleNamespace(),
            shadow_mode=False,
            display_state_path=str(display_path),
        )

        payload = camera_capture_payload("capture-blocked-entry")
        result = await coordinator.camera_capture(payload)
        duplicate = await coordinator.camera_capture(copy.deepcopy(payload))
        state = json.loads(display_path.read_text(encoding="utf-8"))

        self.assertEqual(result["patient_intake"], "blocked")
        self.assertEqual(duplicate["patient_intake"], "blocked")
        self.assertFalse(duplicate["created"])
        self.assertIsNone(result["session"])
        self.assertEqual(self.store.list_sessions(), [])
        self.assertEqual(state["display"]["screen"], "paper_reposition")
        self.assertEqual(state["display"]["capture_id"], "capture-blocked-entry")

    async def test_required_fields_can_start_intake_when_full_text_needs_review(self) -> None:
        profile_id = self.store.create_profile(
            "accepted-entry",
            profile_config(
                patient_input_mode="camera_direct",
                camera_intake_enabled=True,
                camera_patient_enabled=True,
                auto_entry_enabled=True,
                field_resolver={
                    "provider": "rules",
                    "fields": [{
                        "field_key": "patient_id",
                        "target": "patient_id",
                        "enabled": True,
                        "required": True,
                        "label_aliases": [],
                        "relations": ["nearest"],
                        "char_type": "digits",
                        "lengths": [11],
                        "min_ocr_score": 0.7,
                    }],
                },
            ),
            "test",
        )
        self.store.publish_profile(profile_id, "test")
        display_path = self.root / "display-state.json"
        coordinator = ReportCenterCoordinator(
            self.store,
            self.archive,
            SimpleNamespace(),
            shadow_mode=False,
            display_state_path=str(display_path),
        )

        result = await coordinator.camera_capture(
            camera_capture_payload("capture-accepted-entry")
        )
        state = json.loads(display_path.read_text(encoding="utf-8"))

        self.assertEqual(result["patient_result"]["status"], "accepted")
        self.assertEqual(result["patient_intake"], "enabled")
        self.assertEqual(result["session"]["status"], "queued")
        self.assertEqual(result["session"]["patient"]["patient_id"], "60019825336")
        self.assertEqual(state["display"]["screen"], "inputting")

    async def test_successful_entry_writes_log_image_and_display_state(self) -> None:
        session = self._queued_camera_session()
        display_path = self.root / "display-state.json"
        observed = []

        async def entry_handler(query, patient, config):
            observed.append(json.loads(display_path.read_text(encoding="utf-8")))

        coordinator = ReportCenterCoordinator(
            self.store,
            self.archive,
            SimpleNamespace(),
            shadow_mode=False,
            entry_handler=entry_handler,
            intake_only=True,
            display_state_path=str(display_path),
            camera_configuration_image_dir=self.config.camera_configuration_image_dir,
            template_image_dir=self.config.template_image_dir,
            entry_capture_dir=self.config.entry_capture_dir,
        )
        result = await coordinator.process_queue_once()

        self.assertEqual(result["status"], "entry_completed")
        self.assertEqual(observed[0]["display"]["screen"], "inputting")
        self.assertEqual(observed[0]["display"]["patient_name"], "A")
        entry = self.store.list_entry_logs()["items"][0]
        self.assertEqual(entry["status"], "completed")
        self.assertTrue(entry["image_available"])
        self.assertEqual(entry["session_id"], session["id"])
        completed = json.loads(display_path.read_text(encoding="utf-8"))
        self.assertEqual(completed["display"]["screen"], "entry_completed")
        self.assertEqual(completed["display"]["patient_name"], "A")
        self.assertEqual(completed["display"]["capture_id"], session["capture_id"])

    async def test_failed_entry_is_kept_in_log(self) -> None:
        self._queued_camera_session()

        async def entry_handler(query, patient, config):
            raise RuntimeError("HID failed")

        coordinator = ReportCenterCoordinator(
            self.store,
            self.archive,
            SimpleNamespace(),
            shadow_mode=False,
            entry_handler=entry_handler,
            intake_only=True,
            camera_configuration_image_dir=self.config.camera_configuration_image_dir,
            template_image_dir=self.config.template_image_dir,
            entry_capture_dir=self.config.entry_capture_dir,
        )
        result = await coordinator.process_queue_once()

        self.assertEqual(result["status"], "error")
        entry = self.store.list_entry_logs()["items"][0]
        self.assertEqual(entry["status"], "failed")
        self.assertIn("HID failed", entry["error"])

    async def test_restart_recovers_interrupted_hid_session(self) -> None:
        session = self._queued_camera_session()
        session = self.store.transition_session(session["id"], "entering")
        run_id = self.store.begin_workflow_run(
            session["id"], [{"type": "wait", "milliseconds": 0}], 1
        )
        entry_log_id = self.store.begin_entry_log(
            session["id"], session["capture_id"], session["patient"], {}, 1,
            workflow_run_id=run_id,
        )
        marker = Path(self.config.hid_active_marker)
        marker.parent.mkdir(parents=True, exist_ok=True)
        marker.write_text(session["id"], encoding="utf-8")

        recovered_store = ReportCenterStore(self.config.database_path)
        recovered = recovered_store.get_session(session["id"])
        with recovered_store._connect() as connection:
            run = connection.execute(
                "SELECT status,last_error,finished_at FROM workflow_runs WHERE id=?",
                (run_id,),
            ).fetchone()
        entry = recovered_store.get_entry_log(entry_log_id)
        coordinator = ReportCenterCoordinator(
            recovered_store,
            self.archive,
            SimpleNamespace(),
            shadow_mode=False,
            hid_active_marker=str(marker),
        )

        self.assertEqual(recovered["status"], "error")
        self.assertIn("service interrupted", recovered["last_error"])
        self.assertEqual(run["status"], "failed")
        self.assertIn("service interrupted", run["last_error"])
        self.assertIsNotNone(run["finished_at"])
        self.assertEqual(entry["status"], "failed")
        self.assertFalse(marker.exists())
        self.assertIsNotNone(coordinator)


class ReportCenterWebTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.temp = tempfile.TemporaryDirectory()
        root = Path(self.temp.name)
        self.config = make_center_config(root)
        token_file = Path(self.config.external_report_token_file)
        token_file.parent.mkdir(parents=True, exist_ok=True)
        token_file.write_text("test-link-token", encoding="utf-8")
        self.store = ReportCenterStore(self.config.database_path)
        self.store.bootstrap_admin("password123")
        profile_id = self.store.create_profile("default", profile_config(), "test")
        self.store.publish_profile(profile_id, "test")
        archive = ReportArchive(self.config, self.store)
        connectors = SimpleNamespace()
        coordinator = ReportCenterCoordinator(self.store, archive, connectors, shadow_mode=True)
        upload = ReportCenterUploadWorker(self.config, self.store)
        self.web = ReportCenterWeb(self.config, self.store, coordinator, archive, connectors, upload)
        self.client = TestClient(TestServer(self.web.app), cookie_jar=CookieJar(unsafe=True))
        await self.client.start_server()

    async def asyncTearDown(self) -> None:
        await self.client.close()
        self.temp.cleanup()

    async def test_login_csrf_and_manual_intake(self) -> None:
        response = await self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "password123"}
        )
        self.assertEqual(response.status, 200)
        login = await response.json()
        denied = await self.client.post(
            "/api/v1/intake", json={"mode": "manual", "patient": {"patient_name": "A"}}
        )
        self.assertEqual(denied.status, 403)
        changed = await self.client.post(
            "/api/v1/auth/password",
            json={"current_password": "password123", "new_password": "password456"},
            headers={"X-CSRF-Token": login["csrf"]},
        )
        self.assertEqual(changed.status, 200)
        accepted = await self.client.post(
            "/api/v1/intake",
            json={"mode": "manual", "patient": {"patient_name": "A"}},
            headers={"X-CSRF-Token": login["csrf"]},
        )
        self.assertEqual(accepted.status, 201)
        payload = await accepted.json()
        self.assertEqual(payload["status"], "review_required")
        self.assertIn("shadow observation", payload["last_error"])

    async def test_entry_log_api_requires_login_and_serves_retained_image(self) -> None:
        denied = await self.client.get("/api/v1/entry-logs")
        self.assertEqual(denied.status, 401)
        image_root = Path(self.config.entry_capture_dir)
        image_root.mkdir(parents=True)
        image = image_root / "capture-web.jpg"
        image.write_bytes(b"jpeg-web-entry")
        session = self.store.create_session(
            "camera", {"patient_name": "A", "patient_id": "P001"}, capture_id="capture-web"
        )
        log_id = self.store.begin_entry_log(
            session["id"], "capture-web", session["patient"],
            {"patient_name": "A", "patient_id": "P001"}, 2,
            image_path=str(image), image_size=image.stat().st_size,
        )
        self.store.finish_entry_log(log_id, True)
        login = await self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "password123"}
        )
        self.assertEqual(login.status, 200)

        listing = await self.client.get("/api/v1/entry-logs")
        self.assertEqual(listing.status, 200)
        self.assertEqual((await listing.json())["items"][0]["fields"]["patient_id"], "P001")
        detail = await self.client.get("/api/v1/entry-logs/%d" % log_id)
        detail_payload = await detail.json()
        self.assertTrue(detail_payload["image_available"])
        self.assertNotIn("image_path", detail_payload)
        content = await self.client.get("/api/v1/entry-logs/%d/image" % log_id)
        self.assertEqual(content.status, 200)
        self.assertEqual(await content.read(), b"jpeg-web-entry")

    async def test_archive_api_requires_scoped_bearer_token(self) -> None:
        denied = await self.client.get("/archive/v1/reports")
        self.assertEqual(denied.status, 401)
        _, token = self.store.create_api_token("reader", ["reports:read"])
        allowed = await self.client.get(
            "/archive/v1/reports", headers={"Authorization": "Bearer " + token}
        )
        self.assertEqual(allowed.status, 200)

    async def test_camera_callback_records_observation_without_patient_session(self) -> None:
        response = await self.client.post(
            "/internal/v1/camera-captures", json=camera_capture_payload()
        )
        self.assertEqual(response.status, 201)
        result = await response.json()
        self.assertEqual(result["patient_intake"], "disabled")
        self.assertIsNone(result["session"])
        self.assertEqual(self.store.list_sessions(), [])
        duplicate = await self.client.post(
            "/internal/v1/camera-captures", json=camera_capture_payload()
        )
        self.assertEqual(duplicate.status, 200)

    async def test_external_report_callback_requires_token_and_links_patient(self) -> None:
        session = self.store.create_session(
            "camera", {"patient_name": "A"}, capture_id="capture-link"
        )
        self.store.transition_session(session["id"], "entering")
        session = self.store.transition_session(session["id"], "entry_completed")
        request = {
            "report_job_id": 7,
            "pdf_sha256": "c" * 64,
            "source": "printer",
            "created_at": float(session["entered_at"]) + 1,
        }
        denied = await self.client.post("/internal/v1/external-reports", json=request)
        self.assertEqual(denied.status, 403)
        accepted = await self.client.post(
            "/internal/v1/external-reports",
            json=request,
            headers={"X-Internal-Token": "test-link-token"},
        )
        self.assertEqual(accepted.status, 200)
        payload = await accepted.json()
        self.assertEqual(payload["status"], "linked")
        self.assertEqual(payload["patient_session_id"], session["id"])
        self.assertEqual(payload["capture_id"], "capture-link")

    async def test_admin_can_arm_one_full_page_configuration_capture(self) -> None:
        self.store.set_password("admin", "password456")
        login_response = await self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "password456"}
        )
        login = await login_response.json()
        response = await self.client.post(
            "/api/v1/camera/configuration-capture",
            json={},
            headers={"X-CSRF-Token": login["csrf"]},
        )
        self.assertEqual(response.status, 202)
        self.assertTrue(Path(self.config.camera_full_page_once_file).is_file())
        status = await self.client.get("/api/v1/camera/configuration-capture")
        self.assertTrue((await status.json())["armed"])

    async def test_camera_patient_generation_uses_draft_rules_and_persists_json(self) -> None:
        self.store.record_camera_capture(camera_capture_payload())
        self.store.set_password("admin", "password456")
        login_response = await self.client.post(
            "/api/v1/auth/login", json={"username": "admin", "password": "password456"}
        )
        login = await login_response.json()
        response = await self.client.post(
            "/api/v1/camera-captures/capture-001/resolve-patient",
            json={
                "field_resolver": {
                    "provider": "rules",
                    "fields": [{
                        "field_key": "patient_id", "target": "patient_id", "required": True,
                        "label_aliases": [], "relations": ["nearest"],
                        "char_type": "digits", "lengths": [11], "min_ocr_score": 0.7,
                    }],
                },
                "persist": True,
            },
            headers={"X-CSRF-Token": login["csrf"]},
        )
        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertIn(payload["status"], {"accepted", "review_required"})
        self.assertEqual(payload["response"]["data"][0]["patient_id"], "60019825336")
        self.assertTrue(payload["persisted"])
        saved_response = await self.client.get(
            "/api/v1/camera-captures/capture-001/patient"
        )
        self.assertEqual(saved_response.status, 200)
        saved = await saved_response.json()
        self.assertEqual(saved["data"][0]["patient_id"], "60019825336")
        self.assertEqual(self.store.list_sessions(), [])

    async def test_enabled_camera_patient_processing_does_not_create_session_or_query(self) -> None:
        profile_id = self.store.list_profiles()[0]["id"]
        config = profile_config(
            camera_patient_enabled=True,
            field_resolver={
                "provider": "rules",
                "fields": [{
                    "field_key": "patient_id", "target": "patient_id", "enabled": True,
                    "required": True, "label_aliases": [], "char_type": "digits",
                    "lengths": [11], "min_ocr_score": 0.7,
                }],
            },
        )
        self.store.save_profile_draft(profile_id, config, "test")
        self.store.publish_profile(profile_id, "test")
        response = await self.client.post(
            "/internal/v1/camera-captures", json=camera_capture_payload("capture-auto")
        )
        self.assertEqual(response.status, 201)
        payload = await response.json()
        self.assertIsNotNone(payload["patient_result"])
        self.assertEqual(payload["patient_result"]["response"]["data"][0]["patient_id"], "60019825336")
        self.assertEqual(self.store.list_sessions(), [])

    async def test_camera_direct_accepted_fields_create_queued_session(self) -> None:
        profile_id = self.store.list_profiles()[0]["id"]
        config = profile_config(
            patient_input_mode="camera_direct",
            camera_intake_enabled=True,
            field_resolver={
                "provider": "rules",
                "fields": [{
                    "field_key": "patient_id", "target": "patient_id", "enabled": True,
                    "required": True, "label_aliases": [], "char_type": "digits",
                    "lengths": [11], "min_ocr_score": 0.7,
                }],
            },
        )
        self.store.save_profile_draft(profile_id, config, "test")
        self.store.publish_profile(profile_id, "test")
        self.web.coordinator.shadow_mode = False

        session = await self.web.coordinator.intake(
            {
                "mode": "camera_direct",
                "capture_id": "capture-direct-accepted",
                "ocr": camera_capture_payload("capture-direct-accepted"),
            },
            source="camera",
        )

        self.assertEqual(session["status"], "queued")
        self.assertEqual(session["patient"]["patient_id"], "60019825336")

    async def test_loopback_fixed_field_rules_publish_active_profile(self) -> None:
        initial = await self.client.get("/internal/v1/field-rules")
        self.assertEqual(initial.status, 200)
        initial_payload = await initial.json()
        self.assertTrue(initial_payload["available"])

        response = await self.client.post(
            "/internal/v1/field-rules",
            json={
                "fields": [{
                    "field_key": "patient_id",
                    "label": "卡号",
                    "position": "right_then_below",
                    "char_type": "digits",
                    "fixed_length": 11,
                    "min_ocr_score": 0.75,
                    "max_distance": 180,
                    "required": True,
                }]
            },
        )

        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertEqual(payload["schema"]["engine"], "fixed_label_rules")
        self.assertEqual(payload["schema"]["fields"][0]["label"], "卡号")
        resolver = self.store.active_profile_revision()["config"]["field_resolver"]
        self.assertEqual(resolver["matching_mode"], "fixed_label")
        self.assertEqual(resolver["fields"][0]["relations"], [
            "same_text", "same_line_right", "next_line_same_column"
        ])

    async def test_loopback_fixed_field_result_keeps_evidence(self) -> None:
        capture_id = "capture-field-result"
        self.store.record_camera_capture(camera_capture_payload(capture_id))
        active_revision_id = self.store.active_profile_revision()["id"]
        self.store.record_camera_patient_result(
            capture_id,
            active_revision_id,
            {"provider": "rules"},
            {
                "status": "accepted",
                "response": {
                    "code": "SUCCESS", "data": [{"patient_id": "60019825336"}],
                    "msg": "成功", "success": True,
                },
                "evidence": {
                    "patient_id": {
                        "value": "60019825336", "span_ids": [1, 2], "score": 0.98,
                        "relation": "same_line_right", "label": "卡号", "alternatives": [],
                    }
                },
                "missing_fields": [],
                "conflict_fields": [],
            },
            "test",
        )

        response = await self.client.get(
            "/internal/v1/field-result", params={"capture_id": capture_id}
        )

        self.assertEqual(response.status, 200)
        payload = await response.json()
        self.assertTrue(payload["available"])
        self.assertEqual(payload["engine"], "fixed_label_rules")
        self.assertEqual(payload["fields"]["patient_id"]["fixed_label"], "卡号")
        self.assertEqual(payload["fields"]["patient_id"]["source_span_ids"], [1, 2])

    async def test_loopback_field_result_hides_previous_rule_version(self) -> None:
        capture_id = "capture-old-rule"
        self.store.record_camera_capture(camera_capture_payload(capture_id))
        self.store.record_camera_patient_result(
            capture_id,
            None,
            {"provider": "rules"},
            {
                "status": "accepted",
                "response": {"code": "SUCCESS", "data": [], "msg": "成功", "success": True},
                "evidence": {},
                "missing_fields": [],
                "conflict_fields": [],
            },
            "test",
        )

        response = await self.client.get(
            "/internal/v1/field-result", params={"capture_id": capture_id}
        )

        payload = await response.json()
        self.assertFalse(payload["available"])
        self.assertIn("下一张生效", payload["message"])


if __name__ == "__main__":
    unittest.main()
