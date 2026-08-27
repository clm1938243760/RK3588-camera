from __future__ import annotations

import secrets
import time
from dataclasses import dataclass
from typing import Optional


@dataclass
class WebSession:
    token: str
    csrf: str
    username: str
    role: str
    must_change: bool
    expires_at: float


class WebSessionStore:
    def __init__(self, session_hours: int) -> None:
        self.lifetime = max(1, int(session_hours)) * 3600
        self.sessions: dict[str, WebSession] = {}

    def create(self, user: dict[str, object]) -> WebSession:
        self._purge()
        session = WebSession(
            token=secrets.token_urlsafe(32),
            csrf=secrets.token_urlsafe(24),
            username=str(user["username"]),
            role=str(user["role"]),
            must_change=bool(user.get("must_change", False)),
            expires_at=time.time() + self.lifetime,
        )
        self.sessions[session.token] = session
        return session

    def get(self, token: str) -> Optional[WebSession]:
        self._purge()
        session = self.sessions.get(token)
        if session:
            session.expires_at = time.time() + self.lifetime
        return session

    def remove(self, token: str) -> None:
        self.sessions.pop(token, None)

    def _purge(self) -> None:
        now = time.time()
        for token in [key for key, value in self.sessions.items() if value.expires_at <= now]:
            self.sessions.pop(token, None)
