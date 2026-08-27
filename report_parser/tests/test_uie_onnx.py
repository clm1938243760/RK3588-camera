import json
import time
from pathlib import Path

from rk3588_report_parser.uie_onnx import (
    ErnieWordPieceTokenizer,
    _pair_spans,
)
from rk3588_report_parser.uie_patient_service import CameraCaptureFileWatcher


def test_ernie_wordpiece_tokenizer_keeps_original_offsets(tmp_path: Path):
    vocab = tmp_path / "vocab.txt"
    vocab.write_text(
        "\n".join(
            [
                "[PAD]",
                "[CLS]",
                "[SEP]",
                "[MASK]",
                "患",
                "者",
                "id",
                "号",
                "：",
                "abc",
                "##123",
                "[UNK]",
            ]
        )
        + "\n",
        encoding="utf-8",
    )

    pieces = ErnieWordPieceTokenizer(vocab).tokenize_with_offsets("患者ID号：Abc123")

    assert [value.token for value in pieces] == [
        "患",
        "者",
        "id",
        "号",
        "：",
        "abc",
        "##123",
    ]
    assert [(value.start, value.end) for value in pieces] == [
        (0, 1),
        (1, 2),
        (2, 4),
        (4, 5),
        (5, 6),
        (6, 9),
        (9, 12),
    ]


def test_pair_spans_matches_uie_non_overlapping_greedy_pairing():
    pairs = _pair_spans(
        [(3, 0.9), (5, 0.8), (8, 0.7)],
        [(4, 0.6), (7, 0.5), (8, 0.4)],
    )

    assert [(start[0], end[0]) for start, end in pairs] == [(3, 4), (5, 7), (8, 8)]


def test_camera_capture_file_watcher_processes_capture_once(tmp_path: Path):
    result_file = tmp_path / "verified-full-text.json"

    class FakeService:
        def __init__(self):
            self.capture_ids = []

        def parse_capture(self, payload):
            self.capture_ids.append(payload["capture_id"])
            return {"status": "accepted"}

    service = FakeService()
    watcher = CameraCaptureFileWatcher(service, result_file, poll_seconds=0.1)
    watcher.start()
    try:
        payload = {"capture_id": "capture-1", "status": "accepted"}
        result_file.write_text(json.dumps(payload), encoding="utf-8")
        deadline = time.monotonic() + 2.0
        while not service.capture_ids and time.monotonic() < deadline:
            time.sleep(0.05)
        result_file.write_text(json.dumps(payload), encoding="utf-8")
        time.sleep(0.25)
    finally:
        watcher.stop()

    assert service.capture_ids == ["capture-1"]
