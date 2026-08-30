"""Connector state: sent_log (idempotency) + outbox (durable delivery queue).

Lives in the connector's own SQLite DB — Meetily's database is never written.
Payloads are frozen at detection time so delivery is deterministic across
restarts and Meetily DB changes.
"""

from __future__ import annotations

import json
import os
import random
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

from .config import Config
from .logging_setup import log_event

_SCHEMA = """
CREATE TABLE IF NOT EXISTS sent_log (
    meeting_id TEXT PRIMARY KEY,
    disposition TEXT NOT NULL CHECK (disposition IN
        ('delivered', 'skipped_trivial', 'gave_up')),
    delivered_at TEXT,
    payload_sha256 TEXT,
    response_status INTEGER
);
CREATE TABLE IF NOT EXISTS outbox (
    meeting_id TEXT PRIMARY KEY,
    payload_json TEXT NOT NULL,
    created_at TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error TEXT
);
"""

# 4xx statuses that are transient rather than caller error.
_RETRYABLE_4XX = {408, 429}
_PERMANENT_4XX_MAX_ATTEMPTS = 5


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _iso(dt: datetime) -> str:
    return dt.isoformat(timespec="seconds")


class StateStore:
    def __init__(self, cfg: Config):
        state_dir = cfg.state_dir
        state_dir.mkdir(mode=0o700, exist_ok=True)
        self.path = state_dir / "state.db"
        existed = self.path.exists()
        self.conn = sqlite3.connect(self.path, timeout=10)
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.executescript(_SCHEMA)
        if not existed:
            os.chmod(self.path, 0o600)
        self._retry_base = cfg.retry_base_seconds
        self._retry_max = cfg.retry_max_seconds
        self._retry_max_attempts = cfg.retry_max_attempts

    def close(self) -> None:
        self.conn.close()

    # -- idempotency ---------------------------------------------------------

    def seen_meeting_ids(self) -> set[str]:
        rows = self.conn.execute(
            "SELECT meeting_id FROM sent_log UNION SELECT meeting_id FROM outbox"
        ).fetchall()
        return {r[0] for r in rows}

    def mark_skipped_trivial(self, meeting_id: str) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO sent_log (meeting_id, disposition) "
                "VALUES (?, 'skipped_trivial')",
                (meeting_id,),
            )
        log_event("meeting.skipped_trivial", meeting_id=meeting_id)

    # -- outbox --------------------------------------------------------------

    def enqueue(self, meeting_id: str, payload: dict) -> None:
        with self.conn:
            self.conn.execute(
                "INSERT OR IGNORE INTO outbox "
                "(meeting_id, payload_json, created_at, next_attempt_at) "
                "VALUES (?, ?, ?, ?)",
                (meeting_id, json.dumps(payload, ensure_ascii=False),
                 _iso(_now()), _iso(_now())),
            )
        log_event("outbox.enqueued", meeting_id=meeting_id)

    def due(self) -> list[tuple[str, dict, int]]:
        """[(meeting_id, payload, attempts)] whose next attempt is due."""
        rows = self.conn.execute(
            "SELECT meeting_id, payload_json, attempts FROM outbox "
            "WHERE next_attempt_at <= ? ORDER BY created_at",
            (_iso(_now()),),
        ).fetchall()
        return [(r[0], json.loads(r[1]), r[2]) for r in rows]

    def record_success(self, meeting_id: str, status: int, payload_sha256: str) -> None:
        with self.conn:
            self.conn.execute("DELETE FROM outbox WHERE meeting_id = ?", (meeting_id,))
            self.conn.execute(
                "INSERT OR REPLACE INTO sent_log "
                "(meeting_id, disposition, delivered_at, payload_sha256, response_status) "
                "VALUES (?, 'delivered', ?, ?, ?)",
                (meeting_id, _iso(_now()), payload_sha256, status),
            )
        log_event("meeting.delivered", meeting_id=meeting_id, status=status)

    def record_failure(self, meeting_id: str, attempts: int,
                       error: str, http_status: int | None) -> None:
        """Schedule a retry or give up. `error` must be class/status only."""
        attempts += 1
        permanent_4xx = (
            http_status is not None
            and 400 <= http_status < 500
            and http_status not in _RETRYABLE_4XX
        )
        exhausted = (
            (permanent_4xx and attempts >= _PERMANENT_4XX_MAX_ATTEMPTS)
            or (self._retry_max_attempts and attempts >= self._retry_max_attempts)
        )
        if exhausted:
            with self.conn:
                self.conn.execute("DELETE FROM outbox WHERE meeting_id = ?", (meeting_id,))
                self.conn.execute(
                    "INSERT OR REPLACE INTO sent_log "
                    "(meeting_id, disposition, response_status) VALUES (?, 'gave_up', ?)",
                    (meeting_id, http_status),
                )
            log_event("meeting.gave_up", meeting_id=meeting_id,
                      attempts=attempts, error=error, status=http_status)
            return

        delay = min(self._retry_base * (2 ** (attempts - 1)), self._retry_max)
        delay *= 1 + random.uniform(-0.2, 0.2)
        next_at = _now() + timedelta(seconds=delay)
        with self.conn:
            self.conn.execute(
                "UPDATE outbox SET attempts = ?, next_attempt_at = ?, last_error = ? "
                "WHERE meeting_id = ?",
                (attempts, _iso(next_at), error, meeting_id),
            )
        log_event("meeting.retry_scheduled", meeting_id=meeting_id,
                  attempts=attempts, delay_seconds=round(delay), error=error)

    def depth(self) -> int:
        return self.conn.execute("SELECT COUNT(*) FROM outbox").fetchone()[0]

    def forget(self, meeting_id: str) -> bool:
        """Remove a meeting from sent_log + outbox so the watcher re-detects it."""
        with self.conn:
            cur1 = self.conn.execute(
                "DELETE FROM sent_log WHERE meeting_id = ?", (meeting_id,))
            cur2 = self.conn.execute(
                "DELETE FROM outbox WHERE meeting_id = ?", (meeting_id,))
        return bool(cur1.rowcount or cur2.rowcount)


def run_replay(cfg: Config, meeting_id: str) -> int:
    store = StateStore(cfg)
    try:
        if store.forget(meeting_id):
            print(f"forgot {meeting_id}; the watcher will re-detect and re-send it")
            return 0
        print(f"{meeting_id} not found in sent_log or outbox")
        return 1
    finally:
        store.close()
