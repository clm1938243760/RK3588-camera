from __future__ import annotations

import base64
import hashlib
import hmac
import json
import os
import secrets
import sqlite3
import time
import uuid
from contextlib import closing
from pathlib import Path
from typing import Any, Iterable, Optional

from .domain import (
    SESSION_STATES,
    SESSION_TRANSITIONS,
    ConflictError,
    NotFoundError,
    ValidationError,
    canonical_patient,
)


PBKDF2_ITERATIONS = 260000


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS users (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    username TEXT NOT NULL UNIQUE,
    password_hash TEXT NOT NULL,
    role TEXT NOT NULL CHECK(role IN ('admin','operator')),
    enabled INTEGER NOT NULL DEFAULT 1,
    must_change INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS api_tokens (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    token_hash TEXT NOT NULL UNIQUE,
    scopes_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    last_used_at REAL
);
CREATE TABLE IF NOT EXISTS connectors (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL,
    connector_type TEXT NOT NULL,
    config_json TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS profiles (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    name TEXT NOT NULL UNIQUE,
    current_revision_id INTEGER,
    active INTEGER NOT NULL DEFAULT 0,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS profile_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    profile_id INTEGER NOT NULL REFERENCES profiles(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('draft','published','retired')),
    config_json TEXT NOT NULL,
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    published_at REAL,
    UNIQUE(profile_id, version)
);
CREATE TABLE IF NOT EXISTS patients (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    birthday TEXT, exam_item TEXT, ming TEXT, sex TEXT, yue TEXT,
    his_exam_no TEXT, xing TEXT, patient_id TEXT, ri TEXT,
    patient_name TEXT, name_phonetic TEXT, nian TEXT, report_no TEXT, age TEXT,
    extra_fields_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS exam_sessions (
    id TEXT PRIMARY KEY,
    capture_id TEXT UNIQUE,
    query_code TEXT NOT NULL DEFAULT '',
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    patient_id INTEGER REFERENCES patients(id),
    profile_revision_id INTEGER REFERENCES profile_revisions(id),
    config_snapshot_json TEXT NOT NULL,
    exam_item TEXT NOT NULL DEFAULT '',
    review_candidates_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT NOT NULL DEFAULT '',
    report_id INTEGER,
    created_at REAL NOT NULL, updated_at REAL NOT NULL,
    entered_at REAL, completed_at REAL
);
CREATE TABLE IF NOT EXISTS workflow_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES exam_sessions(id) ON DELETE CASCADE,
    revision_id INTEGER REFERENCES profile_revisions(id),
    status TEXT NOT NULL,
    current_step INTEGER NOT NULL DEFAULT 0,
    steps_json TEXT NOT NULL DEFAULT '[]',
    last_error TEXT NOT NULL DEFAULT '',
    started_at REAL NOT NULL, finished_at REAL
);
CREATE TABLE IF NOT EXISTS entry_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL REFERENCES exam_sessions(id) ON DELETE CASCADE,
    capture_id TEXT NOT NULL DEFAULT '',
    workflow_run_id INTEGER REFERENCES workflow_runs(id) ON DELETE SET NULL,
    created_at REAL NOT NULL,
    started_at REAL NOT NULL,
    finished_at REAL,
    status TEXT NOT NULL CHECK(status IN ('running','completed','failed')),
    patient_json TEXT NOT NULL DEFAULT '{}',
    fields_json TEXT NOT NULL DEFAULT '{}',
    action_count INTEGER NOT NULL DEFAULT 0,
    error TEXT NOT NULL DEFAULT '',
    image_path TEXT NOT NULL DEFAULT '',
    image_size INTEGER,
    image_sha256 TEXT NOT NULL DEFAULT '',
    image_error TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT REFERENCES exam_sessions(id),
    source TEXT NOT NULL,
    status TEXT NOT NULL,
    original_name TEXT NOT NULL,
    temp_path TEXT NOT NULL DEFAULT '',
    archive_path TEXT NOT NULL DEFAULT '',
    mime_type TEXT NOT NULL DEFAULT 'application/pdf',
    size INTEGER NOT NULL,
    sha256 TEXT NOT NULL,
    created_at REAL NOT NULL, archived_at REAL, purged_at REAL
);
CREATE TABLE IF NOT EXISTS report_metadata_revisions (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    version INTEGER NOT NULL,
    metadata_json TEXT NOT NULL,
    reason TEXT NOT NULL DEFAULT '',
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(report_id, version)
);
CREATE TABLE IF NOT EXISTS upload_jobs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL REFERENCES reports(id) ON DELETE CASCADE,
    metadata_revision_id INTEGER NOT NULL REFERENCES report_metadata_revisions(id),
    target_connector_id INTEGER REFERENCES connectors(id),
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at REAL NOT NULL DEFAULT 0,
    last_attempt_at REAL,
    last_http_status INTEGER,
    last_error TEXT NOT NULL DEFAULT '',
    last_response TEXT NOT NULL DEFAULT '',
    pdf_sha256 TEXT NOT NULL,
    report_info_xml BLOB NOT NULL DEFAULT X'',
    target_snapshot_json TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL, updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS orphan_reports (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    report_id INTEGER NOT NULL UNIQUE REFERENCES reports(id) ON DELETE CASCADE,
    reason TEXT NOT NULL,
    source_batch_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    resolved_at REAL
);
CREATE TABLE IF NOT EXISTS external_report_links (
    pdf_sha256 TEXT PRIMARY KEY,
    report_job_id INTEGER NOT NULL,
    source TEXT NOT NULL,
    report_created_at REAL NOT NULL,
    patient_session_id TEXT REFERENCES exam_sessions(id),
    capture_id TEXT NOT NULL DEFAULT '',
    status TEXT NOT NULL CHECK(status IN ('linked','unlinked')),
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS camera_captures (
    capture_id TEXT PRIMARY KEY,
    capture_status TEXT NOT NULL,
    schema_version INTEGER NOT NULL,
    payload_sha256 TEXT NOT NULL,
    image_sha256 TEXT NOT NULL DEFAULT '',
    block_count INTEGER NOT NULL DEFAULT 0,
    line_count INTEGER NOT NULL DEFAULT 0,
    average_confidence REAL,
    payload_json TEXT NOT NULL,
    session_id TEXT REFERENCES exam_sessions(id),
    source_created_at REAL,
    received_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS camera_patient_results (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    capture_id TEXT NOT NULL REFERENCES camera_captures(capture_id) ON DELETE CASCADE,
    profile_revision_id INTEGER REFERENCES profile_revisions(id),
    resolver_sha256 TEXT NOT NULL,
    status TEXT NOT NULL,
    response_json TEXT NOT NULL,
    evidence_json TEXT NOT NULL DEFAULT '{}',
    missing_fields_json TEXT NOT NULL DEFAULT '[]',
    conflict_fields_json TEXT NOT NULL DEFAULT '[]',
    created_by TEXT NOT NULL,
    created_at REAL NOT NULL,
    UNIQUE(capture_id, resolver_sha256)
);
CREATE TABLE IF NOT EXISTS audit_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    actor TEXT NOT NULL,
    action TEXT NOT NULL,
    object_type TEXT NOT NULL,
    object_id TEXT NOT NULL DEFAULT '',
    detail_json TEXT NOT NULL DEFAULT '{}',
    remote TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_sessions_status_created ON exam_sessions(status, created_at);
CREATE INDEX IF NOT EXISTS idx_reports_created ON reports(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_reports_sha ON reports(sha256);
CREATE INDEX IF NOT EXISTS idx_upload_ready ON upload_jobs(status, next_attempt_at, id);
CREATE INDEX IF NOT EXISTS idx_patients_search ON patients(patient_name, patient_id, his_exam_no, report_no);
CREATE INDEX IF NOT EXISTS idx_camera_captures_received ON camera_captures(received_at DESC);
CREATE INDEX IF NOT EXISTS idx_camera_patient_capture ON camera_patient_results(capture_id,id DESC);
CREATE INDEX IF NOT EXISTS idx_entry_logs_created ON entry_logs(created_at DESC, id DESC);
CREATE INDEX IF NOT EXISTS idx_entry_logs_status ON entry_logs(status, created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_external_report_patient
    ON external_report_links(patient_session_id) WHERE status='linked';
"""


class ReportCenterStore:
    def __init__(self, database_path: str) -> None:
        self.path = Path(database_path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        connection.execute("PRAGMA busy_timeout=30000")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection:
            connection.executescript(SCHEMA_SQL)
            profile_columns = {row[1] for row in connection.execute("PRAGMA table_info(profiles)")}
            if "active" not in profile_columns:
                connection.execute("ALTER TABLE profiles ADD COLUMN active INTEGER NOT NULL DEFAULT 0")
            connection.execute("PRAGMA user_version=5")
            connection.execute(
                "UPDATE upload_jobs SET status='retry_wait', next_attempt_at=0 WHERE status='uploading'"
            )
            interrupted_at = time.time()
            interrupted_error = "service interrupted before HID entry completed"
            connection.execute(
                """UPDATE exam_sessions SET status='error',last_error=?,updated_at=?
                   WHERE status='entering'""",
                (interrupted_error, interrupted_at),
            )
            connection.execute(
                """UPDATE workflow_runs SET status='failed',last_error=?,finished_at=?
                   WHERE status='running'""",
                (interrupted_error, interrupted_at),
            )
            connection.execute(
                """UPDATE entry_logs SET status='failed',finished_at=?,
                   error=CASE WHEN error='' THEN 'service interrupted before completion' ELSE error END
                   WHERE status='running'""",
                (interrupted_at,),
            )
            connection.commit()
        try:
            os.chmod(self.path, 0o600)
        except OSError:
            pass

    def bootstrap_admin(self, password: str) -> Optional[str]:
        with closing(self._connect()) as connection:
            exists = connection.execute("SELECT 1 FROM users LIMIT 1").fetchone()
        if exists:
            return None
        generated = password or secrets.token_urlsafe(15)
        self.create_user("admin", generated, "admin", must_change=True)
        return generated

    def create_user(self, username: str, password: str, role: str, must_change: bool = False) -> int:
        username = username.strip()
        if not username or role not in {"admin", "operator"}:
            raise ValidationError("invalid user")
        if len(password) < 8 or len(password) > 256:
            raise ValidationError("password must contain 8 to 256 characters")
        now = time.time()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO users(username,password_hash,role,must_change,created_at,updated_at) VALUES(?,?,?,?,?,?)",
                (username, _password_hash(password), role, int(must_change), now, now),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def verify_user(self, username: str, password: str) -> Optional[dict[str, Any]]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id,username,password_hash,role,must_change FROM users WHERE username=? AND enabled=1",
                (username,),
            ).fetchone()
        if row is None or not _verify_password(password, str(row["password_hash"])):
            return None
        return {key: row[key] for key in ("id", "username", "role", "must_change")}

    def list_users(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id,username,role,enabled,must_change,created_at,updated_at FROM users ORDER BY id"
            ).fetchall()
        return [
            {
                "id": int(row["id"]), "username": row["username"], "role": row["role"],
                "enabled": bool(row["enabled"]), "must_change": bool(row["must_change"]),
                "created_at": row["created_at"], "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def set_password(self, username: str, password: str, must_change: bool = False) -> None:
        if len(password) < 8 or len(password) > 256:
            raise ValidationError("password must contain 8 to 256 characters")
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE users SET password_hash=?,must_change=?,updated_at=? WHERE username=? AND enabled=1",
                (_password_hash(password), int(must_change), time.time(), username),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise NotFoundError("user not found")

    def create_api_token(self, name: str, scopes: Iterable[str]) -> tuple[int, str]:
        allowed = {"reports:read", "reports:download"}
        selected = sorted(set(scopes) & allowed)
        if not name.strip() or not selected:
            raise ValidationError("token name and scope are required")
        token = "rc_" + secrets.token_urlsafe(32)
        now = time.time()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO api_tokens(name,token_hash,scopes_json,created_at) VALUES(?,?,?,?)",
                (name.strip(), _token_hash(token), _json(selected), now),
            )
            connection.commit()
            return int(cursor.lastrowid), token

    def verify_api_token(self, token: str, required_scope: str) -> bool:
        digest = _token_hash(token)
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id,scopes_json FROM api_tokens WHERE token_hash=? AND enabled=1", (digest,)
            ).fetchone()
            if row is None or required_scope not in _loads(row["scopes_json"], []):
                return False
            connection.execute("UPDATE api_tokens SET last_used_at=? WHERE id=?", (time.time(), row["id"]))
            connection.commit()
        return True

    def list_api_tokens(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id,name,scopes_json,enabled,created_at,last_used_at FROM api_tokens ORDER BY id DESC"
            ).fetchall()
        return [
            {
                "id": int(row["id"]), "name": row["name"],
                "scopes": _loads(row["scopes_json"], []), "enabled": bool(row["enabled"]),
                "created_at": row["created_at"], "last_used_at": row["last_used_at"],
            }
            for row in rows
        ]

    def revoke_api_token(self, token_id: int) -> None:
        with closing(self._connect()) as connection:
            cursor = connection.execute("UPDATE api_tokens SET enabled=0 WHERE id=?", (token_id,))
            connection.commit()
            if cursor.rowcount == 0:
                raise NotFoundError("API token not found")

    def ensure_default_profile(self, snapshot: dict[str, Any], actor: str = "system") -> int:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT current_revision_id FROM profiles ORDER BY id LIMIT 1"
            ).fetchone()
        if row and row["current_revision_id"]:
            return int(row["current_revision_id"])
        profile_id = self.create_profile(str(snapshot.get("name") or "default"), snapshot, actor)
        return self.publish_profile(profile_id, actor)

    def create_profile(self, name: str, config: dict[str, Any], actor: str) -> int:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            active = 0 if connection.execute("SELECT 1 FROM profiles WHERE active=1 LIMIT 1").fetchone() else 1
            cursor = connection.execute(
                "INSERT INTO profiles(name,active,created_at,updated_at) VALUES(?,?,?,?)", (name.strip(), active, now, now)
            )
            profile_id = int(cursor.lastrowid)
            connection.execute(
                "INSERT INTO profile_revisions(profile_id,version,status,config_json,created_by,created_at) VALUES(?,1,'draft',?,?,?)",
                (profile_id, _json(config), actor, now),
            )
            connection.commit()
            return profile_id

    def save_profile_draft(self, profile_id: int, config: dict[str, Any], actor: str) -> int:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            profile = connection.execute("SELECT id FROM profiles WHERE id=?", (profile_id,)).fetchone()
            if profile is None:
                raise NotFoundError("profile not found")
            version = int(connection.execute(
                "SELECT COALESCE(MAX(version),0)+1 FROM profile_revisions WHERE profile_id=?", (profile_id,)
            ).fetchone()[0])
            cursor = connection.execute(
                "INSERT INTO profile_revisions(profile_id,version,status,config_json,created_by,created_at) VALUES(?,?,'draft',?,?,?)",
                (profile_id, version, _json(config), actor, now),
            )
            connection.execute("UPDATE profiles SET updated_at=? WHERE id=?", (now, profile_id))
            connection.commit()
            return int(cursor.lastrowid)

    def publish_profile(self, profile_id: int, actor: str, revision_id: Optional[int] = None) -> int:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            busy = connection.execute(
                "SELECT 1 FROM exam_sessions WHERE status IN ('entering','awaiting_report','archiving') LIMIT 1"
            ).fetchone()
            if busy:
                raise ConflictError("profile cannot be published while the device is busy")
            if revision_id is None:
                row = connection.execute(
                    "SELECT id FROM profile_revisions WHERE profile_id=? AND status='draft' ORDER BY version DESC LIMIT 1",
                    (profile_id,),
                ).fetchone()
            else:
                row = connection.execute(
                    "SELECT id FROM profile_revisions WHERE id=? AND profile_id=?", (revision_id, profile_id)
                ).fetchone()
            if row is None:
                raise NotFoundError("profile revision not found")
            selected = int(row["id"])
            connection.execute(
                "UPDATE profile_revisions SET status='retired' WHERE profile_id=? AND status='published'",
                (profile_id,),
            )
            connection.execute(
                "UPDATE profile_revisions SET status='published',published_at=? WHERE id=?", (now, selected)
            )
            connection.execute(
                "UPDATE profiles SET current_revision_id=?,updated_at=? WHERE id=?", (selected, now, profile_id)
            )
            connection.execute("UPDATE profiles SET active=CASE WHEN id=? THEN 1 ELSE 0 END", (profile_id,))
            connection.commit()
            return selected

    def list_profiles(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT p.id,p.name,p.current_revision_id,p.active,p.updated_at,r.version,r.config_json
                   FROM profiles p LEFT JOIN profile_revisions r ON r.id=p.current_revision_id ORDER BY p.id"""
            ).fetchall()
        return [
            {
                "id": int(row["id"]), "name": row["name"],
                "current_revision_id": row["current_revision_id"], "version": row["version"],
                "active": bool(row["active"]), "config": _loads(row["config_json"], {}), "updated_at": row["updated_at"],
            }
            for row in rows
        ]

    def get_profile(self, profile_id: int) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            profile = connection.execute("SELECT * FROM profiles WHERE id=?", (profile_id,)).fetchone()
            revisions = connection.execute(
                "SELECT * FROM profile_revisions WHERE profile_id=? ORDER BY version DESC", (profile_id,)
            ).fetchall()
        if profile is None:
            raise NotFoundError("profile not found")
        return {
            "id": int(profile["id"]), "name": profile["name"], "active": bool(profile["active"]),
            "current_revision_id": profile["current_revision_id"], "updated_at": profile["updated_at"],
            "revisions": [
                {
                    "id": int(row["id"]), "version": int(row["version"]), "status": row["status"],
                    "config": _loads(row["config_json"], {}), "created_by": row["created_by"],
                    "created_at": row["created_at"], "published_at": row["published_at"],
                }
                for row in revisions
            ],
        }

    def active_profile_revision(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT r.id,r.profile_id,r.version,r.config_json,p.name
                   FROM profiles p JOIN profile_revisions r ON r.id=p.current_revision_id
                   WHERE p.active=1 ORDER BY p.id LIMIT 1"""
            ).fetchone()
        if row is None:
            raise NotFoundError("no published profile")
        return {
            "id": int(row["id"]), "profile_id": int(row["profile_id"]),
            "version": int(row["version"]), "name": row["name"],
            "config": _loads(row["config_json"], {}),
        }

    def create_connector(self, name: str, connector_type: str, config: dict[str, Any]) -> int:
        if connector_type not in {"sql_proxy", "rest_json", "report_multipart", "rest_multipart"}:
            raise ValidationError("unsupported connector type")
        now = time.time()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO connectors(name,connector_type,config_json,created_at,updated_at) VALUES(?,?,?,?,?)",
                (name.strip(), connector_type, _json(config), now, now),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def list_connectors(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute("SELECT * FROM connectors ORDER BY id").fetchall()
        return [_connector_row(row) for row in rows]

    def get_connector(self, connector_id: int) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM connectors WHERE id=?", (connector_id,)).fetchone()
        if row is None:
            raise NotFoundError("connector not found")
        return _connector_row(row)

    def update_connector(self, connector_id: int, name: str, config: dict[str, Any], enabled: bool = True) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE connectors SET name=?,config_json=?,enabled=?,updated_at=? WHERE id=?",
                (name.strip(), _json(config), int(enabled), time.time(), connector_id),
            )
            connection.commit()
            if cursor.rowcount == 0:
                raise NotFoundError("connector not found")
        return self.get_connector(connector_id)

    def create_patient(self, record: dict[str, Any]) -> int:
        patient = canonical_patient(record)
        now = time.time()
        values = [patient.get(field) for field in (
            "birthday", "exam_item", "ming", "sex", "yue", "his_exam_no", "xing",
            "patient_id", "ri", "patient_name", "name_phonetic", "nian", "report_no", "age",
        )]
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO patients(
                    birthday,exam_item,ming,sex,yue,his_exam_no,xing,patient_id,ri,
                    patient_name,name_phonetic,nian,report_no,age,extra_fields_json,created_at,updated_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (*values, _json(patient["extra_fields"]), now, now),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def create_session(
        self,
        source: str,
        patient: Optional[dict[str, Any]],
        query_code: str = "",
        capture_id: Optional[str] = None,
        status: str = "queued",
        review_candidates: Optional[list[dict[str, Any]]] = None,
    ) -> dict[str, Any]:
        if status not in SESSION_STATES:
            raise ValidationError("invalid session status")
        revision = self.active_profile_revision()
        patient_row_id = self.create_patient(patient) if patient else None
        session_id = uuid.uuid4().hex
        now = time.time()
        with closing(self._connect()) as connection:
            try:
                connection.execute(
                    """INSERT INTO exam_sessions(
                        id,capture_id,query_code,source,status,patient_id,profile_revision_id,
                        config_snapshot_json,exam_item,review_candidates_json,created_at,updated_at
                    ) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)""",
                    (
                        session_id, capture_id, query_code, source, status, patient_row_id,
                        revision["id"], _json(revision["config"]),
                        str((patient or {}).get("exam_item") or ""), _json(review_candidates or []), now, now,
                    ),
                )
                connection.commit()
            except sqlite3.IntegrityError as exc:
                if capture_id:
                    existing = self.get_session_by_capture(capture_id)
                    if existing:
                        return existing
                raise ConflictError("duplicate session") from exc
        return self.get_session(session_id)

    def get_session_by_capture(self, capture_id: str) -> Optional[dict[str, Any]]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT id FROM exam_sessions WHERE capture_id=?", (capture_id,)).fetchone()
        return self.get_session(str(row["id"])) if row else None

    def record_camera_capture(self, payload: dict[str, Any]) -> tuple[dict[str, Any], bool]:
        capture_id = str(payload.get("capture_id", "")).strip()
        if not capture_id or len(capture_id) > 128 or any(
            character not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_."
            for character in capture_id
        ):
            raise ValidationError("camera capture_id is invalid")
        status = str(payload.get("status", "")).strip()
        if status not in {"accepted", "review_required", "rejected", "error"}:
            raise ValidationError("camera capture status is invalid")
        document = payload.get("document")
        if not isinstance(document, dict) or document.get("schema_version") not in {2, "2"}:
            raise ValidationError("camera document schema_version 2 is required")
        blocks = document.get("blocks", [])
        lines = document.get("lines", [])
        if not isinstance(blocks, list) or not isinstance(lines, list):
            raise ValidationError("camera document blocks and lines must be arrays")
        if len(blocks) > 10000 or len(lines) > 10000:
            raise ValidationError("camera OCR evidence is too large")
        scores = [
            float(block["score"])
            for block in blocks
            if isinstance(block, dict)
            and isinstance(block.get("score"), (int, float))
            and 0 <= float(block["score"]) <= 1
        ]
        average_confidence = sum(scores) / len(scores) if scores else None
        source = payload.get("source") if isinstance(payload.get("source"), dict) else {}
        image_sha256 = str(source.get("selected_frame_sha256", "")).strip().lower()
        if image_sha256 and (
            len(image_sha256) != 64 or any(character not in "0123456789abcdef" for character in image_sha256)
        ):
            raise ValidationError("camera selected frame SHA-256 is invalid")
        source_created_at = payload.get("created_at")
        if not isinstance(source_created_at, (int, float)):
            source_created_at = None
        payload_json = _json(payload)
        payload_sha256 = hashlib.sha256(_canonical_json(payload).encode("utf-8")).hexdigest()
        received_at = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM camera_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                if str(existing["payload_sha256"]) != payload_sha256:
                    raise ConflictError("camera capture_id already has different evidence")
                return _camera_capture_row(existing, include_payload=True), False
            connection.execute(
                """INSERT INTO camera_captures(
                    capture_id,capture_status,schema_version,payload_sha256,image_sha256,
                    block_count,line_count,average_confidence,payload_json,source_created_at,received_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?,?)""",
                (
                    capture_id, status, 2, payload_sha256, image_sha256, len(blocks), len(lines),
                    average_confidence, payload_json, source_created_at, received_at,
                ),
            )
            row = connection.execute(
                "SELECT * FROM camera_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            connection.commit()
        return _camera_capture_row(row, include_payload=True), True

    def link_camera_capture_session(self, capture_id: str, session_id: str) -> None:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "UPDATE camera_captures SET session_id=? WHERE capture_id=? AND session_id IS NULL",
                (session_id, capture_id),
            )
            connection.commit()
            if cursor.rowcount == 0:
                row = connection.execute(
                    "SELECT session_id FROM camera_captures WHERE capture_id=?", (capture_id,)
                ).fetchone()
                if row is None:
                    raise NotFoundError("camera capture not found")
                if str(row["session_id"] or "") != session_id:
                    raise ConflictError("camera capture is already linked to another session")

    def get_camera_capture(self, capture_id: str, include_payload: bool = True) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT c.*,r.id AS patient_result_id,r.status AS patient_result_status
                   FROM camera_captures c LEFT JOIN camera_patient_results r ON r.id=(
                     SELECT id FROM camera_patient_results WHERE capture_id=c.capture_id
                     ORDER BY id DESC LIMIT 1)
                   WHERE c.capture_id=?""",
                (capture_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("camera capture not found")
        return _camera_capture_row(row, include_payload=include_payload)

    def list_camera_captures(self, limit: int = 100) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT c.*,r.id AS patient_result_id,r.status AS patient_result_status
                   FROM camera_captures c LEFT JOIN camera_patient_results r ON r.id=(
                     SELECT id FROM camera_patient_results WHERE capture_id=c.capture_id
                     ORDER BY id DESC LIMIT 1)
                   ORDER BY c.received_at DESC LIMIT ?""",
                (max(1, min(int(limit), 500)),),
            ).fetchall()
        return [_camera_capture_row(row, include_payload=False) for row in rows]

    def record_camera_patient_result(
        self,
        capture_id: str,
        profile_revision_id: Optional[int],
        resolver_config: dict[str, Any],
        result: dict[str, Any],
        created_by: str,
    ) -> tuple[dict[str, Any], bool]:
        resolver_sha256 = hashlib.sha256(
            _canonical_json({"resolver_version": 1, "config": resolver_config}).encode("utf-8")
        ).hexdigest()
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            capture = connection.execute(
                "SELECT 1 FROM camera_captures WHERE capture_id=?", (capture_id,)
            ).fetchone()
            if capture is None:
                raise NotFoundError("camera capture not found")
            existing = connection.execute(
                "SELECT * FROM camera_patient_results WHERE capture_id=? AND resolver_sha256=?",
                (capture_id, resolver_sha256),
            ).fetchone()
            if existing is not None:
                connection.commit()
                return _camera_patient_result_row(existing), False
            cursor = connection.execute(
                """INSERT INTO camera_patient_results(
                    capture_id,profile_revision_id,resolver_sha256,status,response_json,evidence_json,
                    missing_fields_json,conflict_fields_json,created_by,created_at
                ) VALUES(?,?,?,?,?,?,?,?,?,?)""",
                (
                    capture_id, profile_revision_id, resolver_sha256, result["status"],
                    _json(result["response"]), _json(result["evidence"]),
                    _json(result["missing_fields"]), _json(result["conflict_fields"]),
                    created_by, now,
                ),
            )
            row = connection.execute(
                "SELECT * FROM camera_patient_results WHERE id=?", (cursor.lastrowid,)
            ).fetchone()
            connection.commit()
        return _camera_patient_result_row(row), True

    def latest_camera_patient_result(self, capture_id: str) -> Optional[dict[str, Any]]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM camera_patient_results WHERE capture_id=? ORDER BY id DESC LIMIT 1",
                (capture_id,),
            ).fetchone()
        return _camera_patient_result_row(row) if row is not None else None

    def latest_camera_patient_result_any(self) -> Optional[dict[str, Any]]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT * FROM camera_patient_results ORDER BY id DESC LIMIT 1"
            ).fetchone()
        return _camera_patient_result_row(row) if row is not None else None

    def get_session(self, session_id: str) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT s.*,p.birthday,p.exam_item AS patient_exam_item,p.ming,p.sex,p.yue,
                   p.his_exam_no,p.xing,p.patient_id AS patient_identifier,p.ri,p.patient_name,
                   p.name_phonetic,p.nian,p.report_no,p.age,p.extra_fields_json
                   FROM exam_sessions s LEFT JOIN patients p ON p.id=s.patient_id WHERE s.id=?""",
                (session_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("session not found")
        return _session_row(row)

    def list_sessions(self, limit: int = 100, status: str = "") -> list[dict[str, Any]]:
        parameters: list[Any] = []
        where = ""
        if status:
            where = "WHERE s.status=?"
            parameters.append(status)
        parameters.append(max(1, min(int(limit), 500)))
        with closing(self._connect()) as connection:
            rows = connection.execute(
                f"""SELECT s.*,p.patient_name,p.patient_id AS patient_identifier,p.his_exam_no,p.report_no
                    FROM exam_sessions s LEFT JOIN patients p ON p.id=s.patient_id
                    {where} ORDER BY s.created_at DESC LIMIT ?""",
                parameters,
            ).fetchall()
        return [_session_summary(row) for row in rows]

    def next_queued_session(self, ignore_report_wait: bool = False) -> Optional[dict[str, Any]]:
        with closing(self._connect()) as connection:
            blocking = ("entering",) if ignore_report_wait else ("entering", "awaiting_report", "archiving")
            placeholders = ",".join("?" for _ in blocking)
            active = connection.execute(
                f"SELECT 1 FROM exam_sessions WHERE status IN ({placeholders}) LIMIT 1",
                blocking,
            ).fetchone()
            if active:
                return None
            row = connection.execute(
                "SELECT id FROM exam_sessions WHERE status='queued' ORDER BY created_at,id LIMIT 1"
            ).fetchone()
        return self.get_session(str(row["id"])) if row else None

    def active_report_session(self) -> Optional[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT id FROM exam_sessions WHERE status='awaiting_report' ORDER BY entered_at,id"
            ).fetchall()
        if len(rows) != 1:
            return None
        return self.get_session(str(rows[0]["id"]))

    def transition_session(self, session_id: str, new_status: str, error: str = "") -> dict[str, Any]:
        if new_status not in SESSION_STATES:
            raise ValidationError("invalid session status")
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM exam_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise NotFoundError("session not found")
            old_status = str(row["status"])
            if new_status != old_status and new_status not in SESSION_TRANSITIONS.get(old_status, set()):
                raise ConflictError("invalid session transition %s -> %s" % (old_status, new_status))
            entered_at = now if new_status in {"entry_completed", "awaiting_report"} else None
            completed_at = now if new_status in {"completed", "cancelled", "report_missing"} else None
            connection.execute(
                """UPDATE exam_sessions SET status=?,last_error=?,updated_at=?,
                   entered_at=COALESCE(?,entered_at),completed_at=COALESCE(?,completed_at) WHERE id=?""",
                (new_status, error[:1000], now, entered_at, completed_at, session_id),
            )
            connection.commit()
        return self.get_session(session_id)

    def associate_external_report(
        self,
        report_job_id: int,
        pdf_sha256: str,
        source: str,
        report_created_at: float,
        window_seconds: int = 7200,
    ) -> dict[str, Any]:
        digest = str(pdf_sha256).strip().lower()
        if len(digest) != 64 or any(char not in "0123456789abcdef" for char in digest):
            raise ValidationError("pdf_sha256 must be a 64-character hexadecimal digest")
        source = str(source).strip().lower()
        if source not in {"msc", "printer", "unknown"}:
            raise ValidationError("external report source must be msc, printer, or unknown")
        created = float(report_created_at)
        if created <= 0:
            raise ValidationError("created_at must be a positive timestamp")
        window = max(60, int(window_seconds))
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            existing = connection.execute(
                "SELECT * FROM external_report_links WHERE pdf_sha256=?", (digest,)
            ).fetchone()
            if existing is not None:
                connection.commit()
                return dict(existing)
            session = connection.execute(
                """SELECT s.id,s.capture_id FROM exam_sessions s
                   WHERE s.status='entry_completed' AND s.entered_at IS NOT NULL
                     AND s.entered_at<=? AND s.entered_at>=?
                     AND NOT EXISTS (
                       SELECT 1 FROM external_report_links l
                       WHERE l.patient_session_id=s.id AND l.status='linked'
                     )
                   ORDER BY s.entered_at DESC,s.id DESC LIMIT 1""",
                (created, created - window),
            ).fetchone()
            session_id = str(session["id"]) if session is not None else None
            capture_id = str(session["capture_id"] or "") if session is not None else ""
            status = "linked" if session is not None else "unlinked"
            connection.execute(
                """INSERT INTO external_report_links(
                     pdf_sha256,report_job_id,source,report_created_at,patient_session_id,
                     capture_id,status,created_at,updated_at
                   ) VALUES(?,?,?,?,?,?,?,?,?)""",
                (
                    digest, int(report_job_id), source, created, session_id,
                    capture_id, status, now, now,
                ),
            )
            if session_id is not None:
                connection.execute(
                    """UPDATE exam_sessions SET status='completed',completed_at=?,updated_at=?
                       WHERE id=? AND status='entry_completed'""",
                    (now, now, session_id),
                )
            row = connection.execute(
                "SELECT * FROM external_report_links WHERE pdf_sha256=?", (digest,)
            ).fetchone()
            connection.commit()
        result = dict(row)
        self.audit(
            "system",
            "external_report.%s" % status,
            "external_report",
            digest[:16],
            {"report_job_id": int(report_job_id), "source": source, "session_id": session_id or ""},
        )
        return result

    def approve_session(self, session_id: str, patient: dict[str, Any]) -> dict[str, Any]:
        patient_row_id = self.create_patient(patient)
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT status FROM exam_sessions WHERE id=?", (session_id,)).fetchone()
            if row is None:
                raise NotFoundError("session not found")
            if row["status"] != "review_required":
                raise ConflictError("only a review session can be approved")
            connection.execute(
                """UPDATE exam_sessions SET patient_id=?,status='queued',review_candidates_json='[]',
                   last_error='',updated_at=? WHERE id=?""",
                (patient_row_id, now, session_id),
            )
            connection.commit()
        return self.get_session(session_id)

    def begin_workflow_run(self, session_id: str, steps: list[dict[str, Any]], revision_id: int) -> int:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                "INSERT INTO workflow_runs(session_id,revision_id,status,steps_json,started_at) VALUES(?,?,'running',?,?)",
                (session_id, revision_id, _json(steps), time.time()),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_workflow_run(self, run_id: int, success: bool, current_step: int, error: str = "") -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE workflow_runs SET status=?,current_step=?,last_error=?,finished_at=? WHERE id=?",
                ("completed" if success else "failed", current_step, error[:1000], time.time(), run_id),
            )
            connection.commit()

    def begin_entry_log(
        self,
        session_id: str,
        capture_id: str,
        patient: dict[str, Any],
        fields: dict[str, Any],
        action_count: int,
        workflow_run_id: Optional[int] = None,
        image_path: str = "",
        image_size: Optional[int] = None,
        image_sha256: str = "",
        image_error: str = "",
    ) -> int:
        now = time.time()
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """INSERT INTO entry_logs(
                    session_id,capture_id,workflow_run_id,created_at,started_at,status,
                    patient_json,fields_json,action_count,image_path,image_size,image_sha256,image_error
                ) VALUES(?,?,?,?,?,'running',?,?,?,?,?,?,?)""",
                (
                    session_id,
                    str(capture_id or ""),
                    workflow_run_id,
                    now,
                    now,
                    _json(patient if isinstance(patient, dict) else {}),
                    _json(fields if isinstance(fields, dict) else {}),
                    max(0, int(action_count)),
                    str(image_path or ""),
                    image_size,
                    str(image_sha256 or ""),
                    str(image_error or "")[:1000],
                ),
            )
            connection.commit()
            return int(cursor.lastrowid)

    def finish_entry_log(self, log_id: int, success: bool, error: str = "") -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE entry_logs SET status=?,error=?,finished_at=? WHERE id=?",
                ("completed" if success else "failed", str(error or "")[:1000], time.time(), int(log_id)),
            )
            connection.commit()

    def list_entry_logs(self, filters: Optional[dict[str, Any]] = None) -> dict[str, Any]:
        filters = filters or {}
        page = max(1, int(filters.get("page", 1)))
        page_size = max(1, min(int(filters.get("page_size", 20)), 100))
        clauses: list[str] = []
        params: list[Any] = []
        status = str(filters.get("status", "")).strip()
        if status:
            if status not in {"running", "completed", "failed"}:
                raise ValidationError("invalid entry log status")
            clauses.append("e.status=?")
            params.append(status)
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        with closing(self._connect()) as connection:
            total = int(connection.execute("SELECT COUNT(*) FROM entry_logs e" + where, params).fetchone()[0])
            rows = connection.execute(
                "SELECT e.* FROM entry_logs e" + where
                + " ORDER BY e.created_at DESC,e.id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return {
            "items": [_entry_log_row(row) for row in rows],
            "page": page,
            "page_size": page_size,
            "total": total,
        }

    def get_entry_log(self, log_id: int, include_private_path: bool = False) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT * FROM entry_logs WHERE id=?", (int(log_id),)).fetchone()
        if row is None:
            raise NotFoundError("entry log not found")
        return _entry_log_row(row, include_private_path=include_private_path)

    def reserve_report(
        self, session_id: Optional[str], source: str, original_name: str,
        temp_path: str, size: int, sha256: str, status: str = "incoming",
    ) -> int:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            if session_id:
                duplicate = connection.execute(
                    "SELECT id FROM reports WHERE session_id=? AND sha256=? AND status!='invalid' LIMIT 1",
                    (session_id, sha256),
                ).fetchone()
                if duplicate:
                    raise ConflictError("duplicate report for session")
            cursor = connection.execute(
                """INSERT INTO reports(session_id,source,status,original_name,temp_path,size,sha256,created_at)
                   VALUES(?,?,?,?,?,?,?,?)""",
                (session_id, source, status, original_name, temp_path, size, sha256, now),
            )
            report_id = int(cursor.lastrowid)
            if session_id:
                session = connection.execute("SELECT status FROM exam_sessions WHERE id=?", (session_id,)).fetchone()
                if session is None or session["status"] != "awaiting_report":
                    raise ConflictError("session is not waiting for a report")
                connection.execute(
                    "UPDATE exam_sessions SET status='archiving',updated_at=? WHERE id=?", (now, session_id)
                )
            connection.commit()
            return report_id

    def finalize_report(
        self, report_id: int, archive_path: str, metadata: dict[str, Any], actor: str,
        upload_target: Optional[dict[str, Any]], report_info_xml: bytes,
    ) -> dict[str, Any]:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = connection.execute("SELECT * FROM reports WHERE id=?", (report_id,)).fetchone()
            if report is None:
                raise NotFoundError("report not found")
            connection.execute(
                "UPDATE reports SET status='archived',archive_path=?,temp_path='',archived_at=? WHERE id=?",
                (archive_path, now, report_id),
            )
            revision = connection.execute(
                """INSERT INTO report_metadata_revisions(report_id,version,metadata_json,created_by,created_at)
                   VALUES(?,1,?,?,?)""",
                (report_id, _json(metadata), actor, now),
            )
            revision_id = int(revision.lastrowid)
            if upload_target:
                connection.execute(
                    """INSERT INTO upload_jobs(
                        report_id,metadata_revision_id,target_connector_id,status,pdf_sha256,
                        report_info_xml,target_snapshot_json,created_at,updated_at
                    ) VALUES(?,?,?,'pending',?,?,?,?,?)""",
                    (
                        report_id, revision_id, upload_target.get("id"), report["sha256"],
                        report_info_xml, _json(upload_target), now, now,
                    ),
                )
            session_id = report["session_id"]
            if session_id:
                connection.execute(
                    "UPDATE exam_sessions SET status='completed',report_id=?,updated_at=?,completed_at=? WHERE id=?",
                    (report_id, now, now, session_id),
                )
            else:
                connection.execute(
                    "INSERT INTO orphan_reports(report_id,reason,created_at) VALUES(?,'no awaiting patient',?)",
                    (report_id, now),
                )
            connection.commit()
        return self.get_report(report_id)

    def fail_report(self, report_id: int, reason: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute("SELECT session_id FROM reports WHERE id=?", (report_id,)).fetchone()
            if row is None:
                return
            connection.execute("UPDATE reports SET status='invalid' WHERE id=?", (report_id,))
            if row["session_id"]:
                connection.execute(
                    "UPDATE exam_sessions SET status='awaiting_report',last_error=?,updated_at=? WHERE id=?",
                    (reason[:1000], time.time(), row["session_id"]),
                )
            connection.commit()

    def get_report(self, report_id: int) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                """SELECT r.*,p.patient_name,p.patient_id AS patient_identifier,p.his_exam_no,p.report_no,
                   s.exam_item,u.status AS upload_status,u.attempts,u.last_error AS upload_error,
                   m.id AS metadata_revision_id,m.version AS metadata_version,m.metadata_json
                   FROM reports r LEFT JOIN exam_sessions s ON s.id=r.session_id
                   LEFT JOIN patients p ON p.id=s.patient_id
                   LEFT JOIN upload_jobs u ON u.id=(
                     SELECT id FROM upload_jobs WHERE report_id=r.id ORDER BY id DESC LIMIT 1)
                   LEFT JOIN report_metadata_revisions m ON m.id=(
                     SELECT id FROM report_metadata_revisions WHERE report_id=r.id ORDER BY version DESC LIMIT 1)
                   WHERE r.id=?""",
                (report_id,),
            ).fetchone()
        if row is None:
            raise NotFoundError("report not found")
        result = dict(row)
        result["metadata"] = _loads(result.pop("metadata_json", None), {})
        return result

    def find_report_by_sha256(self, sha256: str) -> Optional[dict[str, Any]]:
        with closing(self._connect()) as connection:
            row = connection.execute(
                "SELECT id FROM reports WHERE sha256=? AND status!='invalid' ORDER BY id LIMIT 1", (sha256,)
            ).fetchone()
        return self.get_report(int(row["id"])) if row else None

    def set_orphan_reason(self, report_id: int, reason: str) -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "UPDATE orphan_reports SET reason=? WHERE report_id=? AND resolved_at IS NULL",
                (reason[:200], report_id),
            )
            connection.commit()

    def list_reports(self, filters: dict[str, Any]) -> dict[str, Any]:
        page = max(1, int(filters.get("page", 1)))
        page_size = max(1, min(int(filters.get("page_size", 20)), 100))
        clauses: list[str] = []
        params: list[Any] = []
        for key, column in (
            ("patient_name", "p.patient_name"), ("patient_id", "p.patient_id"),
            ("his_exam_no", "p.his_exam_no"), ("report_no", "p.report_no"),
            ("exam_item", "s.exam_item"),
        ):
            value = str(filters.get(key, "")).strip()
            if value:
                clauses.append(f"{column} LIKE ? ESCAPE '\\'")
                params.append("%" + _like_escape(value) + "%")
        for key, column in (("source", "r.source"), ("status", "r.status"), ("upload_status", "u.status")):
            value = str(filters.get(key, "")).strip()
            if value:
                clauses.append(f"{column}=?")
                params.append(value)
        for key, op in (("start_at", ">="), ("end_at", "<=")):
            value = filters.get(key)
            if value not in (None, ""):
                clauses.append(f"r.created_at {op} ?")
                params.append(float(value))
        where = " WHERE " + " AND ".join(clauses) if clauses else ""
        joins = """ FROM reports r LEFT JOIN exam_sessions s ON s.id=r.session_id
                    LEFT JOIN patients p ON p.id=s.patient_id
                    LEFT JOIN upload_jobs u ON u.id=(
                      SELECT id FROM upload_jobs WHERE report_id=r.id ORDER BY id DESC LIMIT 1) """
        with closing(self._connect()) as connection:
            total = int(connection.execute("SELECT COUNT(DISTINCT r.id)" + joins + where, params).fetchone()[0])
            rows = connection.execute(
                """SELECT r.id,r.session_id,r.source,r.status,r.original_name,r.archive_path,r.size,r.sha256,
                   r.created_at,r.archived_at,p.patient_name,p.patient_id AS patient_identifier,
                   p.his_exam_no,p.report_no,s.exam_item,u.status AS upload_status,u.attempts"""
                + joins + where + " ORDER BY r.created_at DESC,r.id DESC LIMIT ? OFFSET ?",
                [*params, page_size, (page - 1) * page_size],
            ).fetchall()
        return {"items": [dict(row) for row in rows], "page": page, "page_size": page_size, "total": total}

    def revise_report_metadata(self, report_id: int, metadata: dict[str, Any], reason: str, actor: str) -> dict[str, Any]:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = connection.execute("SELECT sha256 FROM reports WHERE id=?", (report_id,)).fetchone()
            if report is None:
                raise NotFoundError("report not found")
            current = connection.execute(
                "SELECT COALESCE(MAX(version),0) FROM report_metadata_revisions WHERE report_id=?", (report_id,)
            ).fetchone()
            version = int(current[0]) + 1
            revision = connection.execute(
                "INSERT INTO report_metadata_revisions(report_id,version,metadata_json,reason,created_by,created_at) VALUES(?,?,?,?,?,?)",
                (report_id, version, _json(metadata), reason, actor, now),
            )
            previous_job = connection.execute(
                "SELECT * FROM upload_jobs WHERE report_id=? ORDER BY id DESC LIMIT 1", (report_id,)
            ).fetchone()
            if previous_job is not None:
                target = _loads(previous_job["target_snapshot_json"], {})
                target_config = target.get("config", {}) if isinstance(target, dict) else {}
                if isinstance(target_config, dict) and target_config.get("correction_mode", "local_only") == "reupload":
                    connection.execute(
                        """INSERT INTO upload_jobs(
                            report_id,metadata_revision_id,target_connector_id,status,pdf_sha256,
                            report_info_xml,target_snapshot_json,created_at,updated_at
                        ) VALUES(?,?,?,'pending',?,?,?,?,?)""",
                        (
                            report_id, int(revision.lastrowid), previous_job["target_connector_id"],
                            report["sha256"], previous_job["report_info_xml"],
                            previous_job["target_snapshot_json"], now, now,
                        ),
                    )
            connection.commit()
        return self.get_report(report_id)

    def list_report_revisions(self, report_id: int) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            exists = connection.execute("SELECT 1 FROM reports WHERE id=?", (report_id,)).fetchone()
            if exists is None:
                raise NotFoundError("report not found")
            rows = connection.execute(
                "SELECT * FROM report_metadata_revisions WHERE report_id=? ORDER BY version DESC", (report_id,)
            ).fetchall()
        return [
            {
                "id": int(row["id"]), "version": int(row["version"]),
                "metadata": _loads(row["metadata_json"], {}), "reason": row["reason"],
                "created_by": row["created_by"], "created_at": row["created_at"],
            }
            for row in rows
        ]

    def assign_report(self, report_id: int, session_id: str, actor: str) -> dict[str, Any]:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            report = connection.execute("SELECT session_id,status FROM reports WHERE id=?", (report_id,)).fetchone()
            session = connection.execute(
                "SELECT id,status,report_id FROM exam_sessions WHERE id=?", (session_id,)
            ).fetchone()
            if report is None or session is None:
                raise NotFoundError("report or session not found")
            if report["session_id"] and report["session_id"] != session_id:
                raise ConflictError("report is already assigned")
            if session["report_id"] is not None or session["status"] not in {"awaiting_report", "report_missing"}:
                raise ConflictError("target session cannot accept a report")
            connection.execute("UPDATE reports SET session_id=? WHERE id=?", (session_id, report_id))
            connection.execute("UPDATE exam_sessions SET report_id=?,status='completed',completed_at=?,updated_at=? WHERE id=?", (report_id, now, now, session_id))
            connection.execute("UPDATE orphan_reports SET resolved_at=? WHERE report_id=?", (now, report_id))
            connection.commit()
        self.audit(actor, "report.assign", "report", str(report_id), {"session_id": session_id})
        return self.get_report(report_id)

    def retry_upload(self, report_id: int) -> bool:
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                """UPDATE upload_jobs SET status='pending',attempts=0,next_attempt_at=0,last_error='',updated_at=?
                   WHERE report_id=? AND status!='uploaded'""",
                (time.time(), report_id),
            )
            connection.commit()
            return cursor.rowcount > 0

    def next_upload_job(self, max_attempts: int) -> Optional[dict[str, Any]]:
        now = time.time()
        with closing(self._connect()) as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """SELECT u.*,r.archive_path,r.size,r.mime_type FROM upload_jobs u JOIN reports r ON r.id=u.report_id
                   WHERE u.status IN ('pending','retry_wait') AND u.next_attempt_at<=? AND u.attempts<?
                   ORDER BY u.id LIMIT 1""",
                (now, max_attempts),
            ).fetchone()
            if row is None:
                connection.commit()
                return None
            attempts = int(row["attempts"]) + 1
            connection.execute(
                "UPDATE upload_jobs SET status='uploading',attempts=?,last_attempt_at=?,updated_at=? WHERE id=?",
                (attempts, now, now, row["id"]),
            )
            connection.commit()
            result = dict(row)
            result["attempts"] = attempts
            result["target_snapshot"] = _loads(result.pop("target_snapshot_json", None), {})
            return result

    def finish_upload(self, job_id: int, success: bool, max_attempts: int, retry_seconds: int, http_status: Optional[int], error: str, response: str) -> None:
        now = time.time()
        with closing(self._connect()) as connection:
            row = connection.execute("SELECT attempts FROM upload_jobs WHERE id=?", (job_id,)).fetchone()
            if row is None:
                return
            attempts = int(row["attempts"])
            status = "uploaded" if success else "exhausted" if attempts >= max_attempts else "retry_wait"
            next_attempt = 0 if status != "retry_wait" else now + retry_seconds
            connection.execute(
                """UPDATE upload_jobs SET status=?,next_attempt_at=?,last_http_status=?,last_error=?,
                   last_response=?,updated_at=? WHERE id=?""",
                (status, next_attempt, http_status, error[:1000], response[:2000], now, job_id),
            )
            connection.commit()

    def counts(self) -> dict[str, Any]:
        with closing(self._connect()) as connection:
            sessions = {row[0]: int(row[1]) for row in connection.execute("SELECT status,COUNT(*) FROM exam_sessions GROUP BY status")}
            reports = {row[0]: int(row[1]) for row in connection.execute("SELECT status,COUNT(*) FROM reports GROUP BY status")}
            uploads = {row[0]: int(row[1]) for row in connection.execute("SELECT status,COUNT(*) FROM upload_jobs GROUP BY status")}
            camera_captures = {
                row[0]: int(row[1])
                for row in connection.execute(
                    "SELECT capture_status,COUNT(*) FROM camera_captures GROUP BY capture_status"
                )
            }
            camera_patients = {
                row[0]: int(row[1])
                for row in connection.execute(
                    "SELECT status,COUNT(*) FROM camera_patient_results GROUP BY status"
                )
            }
        return {
            "sessions": sessions,
            "reports": reports,
            "uploads": uploads,
            "camera_captures": camera_captures,
            "camera_patients": camera_patients,
        }

    def cleanup_preview(self, retention_days: int) -> list[dict[str, Any]]:
        cutoff = time.time() - max(1, retention_days) * 86400
        with closing(self._connect()) as connection:
            rows = connection.execute(
                """SELECT r.id,r.archive_path,r.size,r.archived_at FROM reports r
                   JOIN upload_jobs u ON u.report_id=r.id
                   WHERE r.status='archived' AND u.status='uploaded' AND r.archived_at<? ORDER BY r.id""",
                (cutoff,),
            ).fetchall()
        return [dict(row) for row in rows]

    def mark_purged(self, report_ids: Iterable[int]) -> int:
        ids = sorted(set(int(value) for value in report_ids))
        if not ids:
            return 0
        placeholders = ",".join("?" for _ in ids)
        with closing(self._connect()) as connection:
            cursor = connection.execute(
                f"UPDATE reports SET status='purged',purged_at=? WHERE id IN ({placeholders}) AND status='archived'",
                [time.time(), *ids],
            )
            connection.commit()
            return int(cursor.rowcount)

    def audit(self, actor: str, action: str, object_type: str, object_id: str = "", detail: Optional[dict[str, Any]] = None, remote: str = "") -> None:
        with closing(self._connect()) as connection:
            connection.execute(
                "INSERT INTO audit_logs(actor,action,object_type,object_id,detail_json,remote,created_at) VALUES(?,?,?,?,?,?,?)",
                (actor, action, object_type, object_id, _json(detail or {}), remote, time.time()),
            )
            connection.commit()

    def list_audit(self, limit: int = 200) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection:
            rows = connection.execute(
                "SELECT * FROM audit_logs ORDER BY id DESC LIMIT ?", (max(1, min(limit, 1000)),)
            ).fetchall()
        result = []
        for row in rows:
            item = dict(row)
            item["detail"] = _loads(item.pop("detail_json", None), {})
            result.append(item)
        return result

    def backup(self, destination: str) -> dict[str, Any]:
        path = Path(destination)
        path.parent.mkdir(parents=True, exist_ok=True)
        with closing(self._connect()) as source, closing(sqlite3.connect(path)) as target:
            source.backup(target)
        try:
            os.chmod(path, 0o600)
        except OSError:
            pass
        return {"path": str(path), "size": path.stat().st_size, "created_at": time.time()}


def _session_row(row: sqlite3.Row) -> dict[str, Any]:
    patient = None
    if row["patient_id"] is not None:
        patient = {
            "birthday": row["birthday"], "exam_item": row["patient_exam_item"], "ming": row["ming"],
            "sex": row["sex"], "yue": row["yue"], "his_exam_no": row["his_exam_no"],
            "xing": row["xing"], "patient_id": row["patient_identifier"], "ri": row["ri"],
            "patient_name": row["patient_name"], "name_phonetic": row["name_phonetic"],
            "nian": row["nian"], "report_no": row["report_no"], "age": row["age"],
            "extra_fields": _loads(row["extra_fields_json"], {}),
        }
    item = _session_summary(row)
    item.update({
        "patient": patient,
        "config_snapshot": _loads(row["config_snapshot_json"], {}),
        "review_candidates": _loads(row["review_candidates_json"], []),
        "last_error": row["last_error"],
    })
    return item


def _entry_log_row(row: sqlite3.Row, include_private_path: bool = False) -> dict[str, Any]:
    image_path = str(row["image_path"] or "")
    item = {
        "id": int(row["id"]),
        "session_id": row["session_id"],
        "capture_id": row["capture_id"],
        "workflow_run_id": row["workflow_run_id"],
        "created_at": row["created_at"],
        "started_at": row["started_at"],
        "finished_at": row["finished_at"],
        "status": row["status"],
        "patient": _loads(row["patient_json"], {}),
        "fields": _loads(row["fields_json"], {}),
        "action_count": int(row["action_count"]),
        "error": row["error"],
        "image_available": bool(image_path and Path(image_path).is_file()),
        "image_size": row["image_size"],
        "image_sha256": row["image_sha256"],
        "image_error": row["image_error"],
    }
    if include_private_path:
        item["image_path"] = image_path
    return item


def _session_summary(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": row["id"], "capture_id": row["capture_id"], "query_code": row["query_code"],
        "source": row["source"], "status": row["status"], "patient_row_id": row["patient_id"],
        "profile_revision_id": row["profile_revision_id"],
        "patient_name": row["patient_name"] if "patient_name" in row.keys() else None,
        "patient_id": row["patient_identifier"] if "patient_identifier" in row.keys() else None,
        "his_exam_no": row["his_exam_no"] if "his_exam_no" in row.keys() else None,
        "report_no": row["report_no"] if "report_no" in row.keys() else None,
        "exam_item": row["exam_item"], "report_id": row["report_id"],
        "created_at": row["created_at"], "updated_at": row["updated_at"],
        "entered_at": row["entered_at"], "completed_at": row["completed_at"],
    }


def _connector_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]), "name": row["name"], "type": row["connector_type"],
        "config": _loads(row["config_json"], {}), "enabled": bool(row["enabled"]),
        "created_at": row["created_at"], "updated_at": row["updated_at"],
    }


def _camera_capture_row(row: sqlite3.Row, include_payload: bool) -> dict[str, Any]:
    result = {
        "capture_id": row["capture_id"],
        "status": row["capture_status"],
        "schema_version": int(row["schema_version"]),
        "payload_sha256": row["payload_sha256"],
        "image_sha256": row["image_sha256"],
        "block_count": int(row["block_count"]),
        "line_count": int(row["line_count"]),
        "average_confidence": row["average_confidence"],
        "session_id": row["session_id"],
        "created_at": row["source_created_at"],
        "received_at": row["received_at"],
        "patient_result_id": row["patient_result_id"] if "patient_result_id" in row.keys() else None,
        "patient_result_status": row["patient_result_status"] if "patient_result_status" in row.keys() else None,
    }
    if include_payload:
        result["payload"] = _loads(row["payload_json"], {})
    return result


def _camera_patient_result_row(row: sqlite3.Row) -> dict[str, Any]:
    return {
        "id": int(row["id"]),
        "capture_id": row["capture_id"],
        "profile_revision_id": row["profile_revision_id"],
        "resolver_sha256": row["resolver_sha256"],
        "status": row["status"],
        "response": _loads(row["response_json"], {}),
        "evidence": _loads(row["evidence_json"], {}),
        "missing_fields": _loads(row["missing_fields_json"], []),
        "conflict_fields": _loads(row["conflict_fields_json"], []),
        "created_by": row["created_by"],
        "created_at": row["created_at"],
    }


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"))


def _canonical_json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def _loads(value: Any, fallback: Any) -> Any:
    if value in (None, ""):
        return fallback
    try:
        return json.loads(str(value))
    except (TypeError, ValueError):
        return fallback


def _password_hash(password: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ITERATIONS)
    return "pbkdf2_sha256$%d$%s$%s" % (
        PBKDF2_ITERATIONS,
        base64.b64encode(salt).decode("ascii"),
        base64.b64encode(digest).decode("ascii"),
    )


def _verify_password(password: str, encoded: str) -> bool:
    try:
        algorithm, rounds, salt_text, digest_text = encoded.split("$", 3)
        if algorithm != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_text)
        expected = base64.b64decode(digest_text)
        actual = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, int(rounds))
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


def _token_hash(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _like_escape(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
