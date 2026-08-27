from __future__ import annotations

import json
import sqlite3
from pathlib import Path
from typing import Any

from .events import GatewayEvent


class EventQueue:
    def __init__(self, sqlite_path: str) -> None:
        self.path = Path(sqlite_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        conn = sqlite3.connect(self.path)
        conn.row_factory = sqlite3.Row
        return conn

    def _init_db(self) -> None:
        with self._connect() as conn:
            conn.execute(
                """
                CREATE TABLE IF NOT EXISTS events (
                    id TEXT PRIMARY KEY,
                    type TEXT NOT NULL,
                    device_id TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    payload TEXT NOT NULL,
                    attempts INTEGER NOT NULL DEFAULT 0,
                    last_error TEXT NOT NULL DEFAULT ''
                )
                """
            )

    def put(self, event: GatewayEvent) -> None:
        data = event.to_dict()
        with self._connect() as conn:
            conn.execute(
                """
                INSERT OR REPLACE INTO events
                (id, type, device_id, created_at, payload, attempts, last_error)
                VALUES (?, ?, ?, ?, ?, COALESCE((SELECT attempts FROM events WHERE id = ?), 0), '')
                """,
                (
                    data["id"],
                    data["type"],
                    data["device_id"],
                    data["created_at"],
                    json.dumps(data["payload"], ensure_ascii=False),
                    data["id"],
                ),
            )

    def get_batch(self, limit: int) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY created_at ASC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "type": row["type"],
                "device_id": row["device_id"],
                "created_at": row["created_at"],
                "payload": json.loads(row["payload"]),
                "attempts": row["attempts"],
                "last_error": row["last_error"],
            }
            for row in rows
        ]

    def list_recent(self, limit: int = 20) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute(
                "SELECT * FROM events ORDER BY created_at DESC LIMIT ?",
                (limit,),
            ).fetchall()
        return [
            {
                "id": row["id"],
                "type": row["type"],
                "device_id": row["device_id"],
                "created_at": row["created_at"],
                "payload": json.loads(row["payload"]),
                "attempts": row["attempts"],
                "last_error": row["last_error"],
            }
            for row in rows
        ]

    def mark_sent(self, event_ids: list[str]) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self._connect() as conn:
            conn.execute(f"DELETE FROM events WHERE id IN ({placeholders})", event_ids)

    def mark_failed(self, event_ids: list[str], error: str) -> None:
        if not event_ids:
            return
        placeholders = ",".join("?" for _ in event_ids)
        with self._connect() as conn:
            conn.execute(
                f"""
                UPDATE events
                SET attempts = attempts + 1, last_error = ?
                WHERE id IN ({placeholders})
                """,
                [error[:500], *event_ids],
            )

    def count(self) -> int:
        with self._connect() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM events").fetchone()[0])
