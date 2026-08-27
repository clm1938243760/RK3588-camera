"""Safe latest-frame reader for rotating camera JPEG snapshots."""

from __future__ import annotations

import glob
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Tuple


FrameKey = Tuple[str, int, int]


@dataclass(frozen=True)
class JpegFrame:
    path: Path
    image_bytes: bytes
    key: FrameKey


class LatestJpegFrameSource:
    def __init__(self, frame_glob: str) -> None:
        if not frame_glob.strip():
            raise ValueError("frame_glob must not be empty")
        self.frame_glob = frame_glob
        self._last_key: Optional[FrameKey] = None

    def read_new(self) -> Optional[JpegFrame]:
        latest = self._latest_candidate()
        if latest is None:
            return None
        path, initial_stat = latest
        key = (str(path), initial_stat.st_mtime_ns, initial_stat.st_size)
        if key == self._last_key:
            return None
        try:
            image_bytes = path.read_bytes()
            final_stat = path.stat()
        except OSError:
            return None
        final_key = (str(path), final_stat.st_mtime_ns, final_stat.st_size)
        if final_key != key:
            return None
        if len(image_bytes) != final_stat.st_size:
            return None
        eoi_position = image_bytes.rfind(b"\xff\xd9")
        if not image_bytes.startswith(b"\xff\xd8") or eoi_position < len(image_bytes) - 4096:
            return None
        self._last_key = key
        return JpegFrame(path=path, image_bytes=image_bytes, key=key)

    def _latest_candidate(self) -> Optional[Tuple[Path, os.stat_result]]:
        candidates = []
        for raw_path in glob.glob(self.frame_glob):
            path = Path(raw_path)
            try:
                stat = path.stat()
            except OSError:
                continue
            if stat.st_size >= 4:
                candidates.append((path, stat))
        if not candidates:
            return None
        return max(candidates, key=lambda item: (item[1].st_mtime_ns, str(item[0])))
