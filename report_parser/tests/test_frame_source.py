from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path

from rk3588_report_parser.frame_source import LatestJpegFrameSource


def jpeg(payload: bytes) -> bytes:
    return b"\xff\xd8" + payload + b"\xff\xd9"


class LatestJpegFrameSourceTests(unittest.TestCase):
    def test_reads_each_complete_version_once(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame_1.jpg"
            path.write_bytes(jpeg(b"one"))
            source = LatestJpegFrameSource(str(Path(directory) / "*.jpg"))

            first = source.read_new()
            self.assertIsNotNone(first)
            self.assertEqual(first.image_bytes, jpeg(b"one"))
            self.assertIsNone(source.read_new())

            path.write_bytes(jpeg(b"second"))
            os.utime(path, ns=(2_000_000_000, 2_000_000_000))
            second = source.read_new()
            self.assertIsNotNone(second)
            self.assertEqual(second.image_bytes, jpeg(b"second"))

    def test_ignores_incomplete_jpeg(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.jpg"
            path.write_bytes(b"not complete")
            source = LatestJpegFrameSource(str(Path(directory) / "*.jpg"))
            self.assertIsNone(source.read_new())

            path.write_bytes(jpeg(b"complete"))
            frame = source.read_new()
            self.assertIsNotNone(frame)
            self.assertEqual(frame.image_bytes, jpeg(b"complete"))

    def test_accepts_small_trailer_after_jpeg_end_marker(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "frame.jpg"
            path.write_bytes(jpeg(b"complete") + b"camera metadata")
            frame = LatestJpegFrameSource(str(Path(directory) / "*.jpg")).read_new()
            self.assertIsNotNone(frame)

    def test_chooses_newest_rotating_file(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            first = Path(directory) / "frame_1.jpg"
            second = Path(directory) / "frame_2.jpg"
            first.write_bytes(jpeg(b"old"))
            second.write_bytes(jpeg(b"new"))
            os.utime(first, ns=(1_000_000_000, 1_000_000_000))
            os.utime(second, ns=(2_000_000_000, 2_000_000_000))

            frame = LatestJpegFrameSource(str(Path(directory) / "*.jpg")).read_new()
            self.assertIsNotNone(frame)
            self.assertEqual(frame.path, second)


if __name__ == "__main__":
    unittest.main()
