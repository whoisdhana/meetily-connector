"""Read-only access to Meetily's SQLite database.

Schema facts verified against upstream v0.4.0 — see docs/schema-notes.md:
- status literals: 'PENDING' (upper) / 'completed' / 'failed' / 'cancelled' (lower)
- meetings.created_at is sqlx UTC text: "2026-08-30 14:23:05.123+00:00" (no 'T')
- transcripts: N segment rows per meeting, ordered by audio_start_time;
  transcripts.timestamp is a date-less HH:MM:SS string — never order by it
- the summary JSON lives in summary_processes.result

The connection is opened mode=ro with query_only as belt and braces; Meetily
may be mid-write, so callers treat DatabaseBusy as "skip and retry later".
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path

STATUS_COMPLETED = "completed"  # exact literal from upstream summary.rs:135


class DatabaseBusy(Exception):
    """Meetily is writing; skip this tick and try again later."""


@dataclass
class MeetingRow:
    id: str
    title: str
    created_at: str  # normalized tz-aware ISO-8601
    updated_at: str
    folder_path: str | None
    summary_status: str | None = None
    summary_updated_at: str | None = None  # normalized ISO, if a summary row exists
    summary_result: str | None = None  # raw JSON text from summary_processes.result


def parse_meetily_datetime(text: str | None) -> datetime | None:
    """Parse sqlx's SQLite datetime text ("YYYY-MM-DD HH:MM:SS.ffffff+00:00").

    fromisoformat (3.11+) accepts the space separator and offset; naive values
    (legacy imports) are reinterpreted as UTC, matching upstream's DateTimeUtc.
    """
    if not text:
        return None
    try:
        dt = datetime.fromisoformat(text)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _iso(text: str | None) -> str | None:
    dt = parse_meetily_datetime(text)
    return dt.isoformat(timespec="seconds") if dt else None


class MeetilyDB:
    def __init__(self, path: Path):
        self.path = path
        uri = f"file:{path}?mode=ro"
        self.conn = sqlite3.connect(uri, uri=True, timeout=5)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA query_only=ON")

    def __enter__(self) -> "MeetilyDB":
        return self

    def __exit__(self, *exc) -> None:
        self.close()

    def close(self) -> None:
        self.conn.close()

    def _execute(self, sql: str, params: tuple = ()) -> list[sqlite3.Row]:
        try:
            return self.conn.execute(sql, params).fetchall()
        except sqlite3.OperationalError as exc:
            message = str(exc).lower()
            if "locked" in message or "busy" in message:
                raise DatabaseBusy(str(exc)) from exc
            raise

    # -- queries -------------------------------------------------------------

    def list_meetings(self, limit: int = 20, since: str | None = None,
                      until: str | None = None) -> list[MeetingRow]:
        sql = (
            "SELECT m.id, m.title, m.created_at, m.updated_at, m.folder_path, "
            "       sp.status AS summary_status, sp.updated_at AS summary_updated_at "
            "FROM meetings m LEFT JOIN summary_processes sp ON sp.meeting_id = m.id "
        )
        clauses, params = [], []
        if since:
            clauses.append("m.created_at >= ?")
            params.append(since)
        if until:
            clauses.append("m.created_at <= ?")
            params.append(until)
        if clauses:
            sql += "WHERE " + " AND ".join(clauses) + " "
        sql += "ORDER BY m.created_at DESC LIMIT ?"
        params.append(min(int(limit), 100))
        return [self._meeting_row(r) for r in self._execute(sql, tuple(params))]

    def get_meeting(self, meeting_id: str) -> MeetingRow | None:
        rows = self._execute(
            "SELECT m.id, m.title, m.created_at, m.updated_at, m.folder_path, "
            "       sp.status AS summary_status, sp.updated_at AS summary_updated_at, "
            "       sp.result AS summary_result "
            "FROM meetings m LEFT JOIN summary_processes sp ON sp.meeting_id = m.id "
            "WHERE m.id = ?",
            (meeting_id,),
        )
        return self._meeting_row(rows[0], with_result=True) if rows else None

    def get_transcript_text(self, meeting_id: str) -> tuple[str, float]:
        """(full text ordered by audio_start_time, duration_seconds)."""
        rows = self._execute(
            "SELECT transcript, audio_start_time, audio_end_time, duration "
            "FROM transcripts WHERE meeting_id = ? "
            "ORDER BY audio_start_time, id",
            (meeting_id,),
        )
        parts = [r["transcript"] for r in rows if r["transcript"]]
        duration = 0.0
        ends = [r["audio_end_time"] for r in rows if r["audio_end_time"] is not None]
        if ends:
            duration = max(ends)
        else:
            duration = sum(r["duration"] or 0.0 for r in rows)
        return "\n".join(parts), duration

    def search_meetings(self, query: str, limit: int = 10) -> list[dict]:
        """Substring search over titles and transcript segments (no FTS upstream)."""
        pattern = f"%{query.lower()}%"
        rows = self._execute(
            "SELECT m.id, m.title, m.created_at, t.transcript "
            "FROM meetings m JOIN transcripts t ON t.meeting_id = m.id "
            "WHERE LOWER(t.transcript) LIKE ? OR LOWER(m.title) LIKE ? "
            "ORDER BY m.created_at DESC",
            (pattern, pattern),
        )
        hits: dict[str, dict] = {}
        for r in rows:
            if r["id"] in hits:
                continue
            snippet = _snippet(r["transcript"] or "", query)
            hits[r["id"]] = {
                "id": r["id"],
                "title": r["title"],
                "created_at": _iso(r["created_at"]),
                "snippet": snippet,
            }
            if len(hits) >= min(int(limit), 50):
                break
        return list(hits.values())

    def completed_unsent(self, seen_ids: set[str]) -> list[MeetingRow]:
        """Meetings whose summary is completed, excluding already-handled ids.

        The seen-set diff happens in Python because sent_log lives in the
        connector's own DB — Meetily's DB is never ATTACHed or written.
        """
        rows = self._execute(
            "SELECT m.id, m.title, m.created_at, m.updated_at, m.folder_path, "
            "       sp.status AS summary_status, sp.updated_at AS summary_updated_at, "
            "       sp.result AS summary_result "
            "FROM summary_processes sp JOIN meetings m ON m.id = sp.meeting_id "
            "WHERE sp.status = ? ORDER BY sp.updated_at",
            (STATUS_COMPLETED,),
        )
        return [self._meeting_row(r, with_result=True)
                for r in rows if r["id"] not in seen_ids]

    def transcript_ready_unsent(self, seen_ids: set[str]) -> list[MeetingRow]:
        """Meetings whose transcript exists, excluding already-handled ids.

        Trigger mode "transcript": upstream writes the meeting row and ALL its
        segments in one transaction when the recording stops, so transcript
        presence means the meeting is finished — no summary generation needed.
        A summary row is joined opportunistically if one happens to exist.
        """
        rows = self._execute(
            "SELECT m.id, m.title, m.created_at, m.updated_at, m.folder_path, "
            "       sp.status AS summary_status, sp.updated_at AS summary_updated_at, "
            "       sp.result AS summary_result "
            "FROM meetings m "
            "LEFT JOIN summary_processes sp ON sp.meeting_id = m.id "
            "WHERE EXISTS (SELECT 1 FROM transcripts t WHERE t.meeting_id = m.id) "
            "ORDER BY m.created_at",
        )
        return [self._meeting_row(r, with_result=True)
                for r in rows if r["id"] not in seen_ids]

    def counts(self) -> dict[str, int]:
        out = {}
        for label, sql in {
            "meetings": "SELECT COUNT(*) FROM meetings",
            "transcript_segments": "SELECT COUNT(*) FROM transcripts",
            "summaries_completed":
                "SELECT COUNT(*) FROM summary_processes WHERE status = 'completed'",
        }.items():
            try:
                out[label] = self._execute(sql)[0][0]
            except sqlite3.Error:
                out[label] = -1  # schema drift; surfaced by connector_status
        return out

    def schema_warnings(self) -> list[str]:
        """Detect drift from the verified v0.4.0 schema."""
        expected = {
            "meetings": {"id", "title", "created_at", "updated_at"},
            "transcripts": {"id", "meeting_id", "transcript", "audio_start_time"},
            "summary_processes": {"meeting_id", "status", "updated_at", "result"},
        }
        warnings = []
        for table, columns in expected.items():
            rows = self._execute(f"PRAGMA table_info({table})")
            present = {r["name"] for r in rows}
            if not present:
                warnings.append(f"table {table} missing")
            elif not columns <= present:
                warnings.append(f"table {table} missing columns {sorted(columns - present)}")
        return warnings

    @staticmethod
    def _meeting_row(r: sqlite3.Row, with_result: bool = False) -> MeetingRow:
        return MeetingRow(
            id=r["id"],
            title=r["title"],
            created_at=_iso(r["created_at"]) or r["created_at"],
            updated_at=_iso(r["updated_at"]) or r["updated_at"],
            folder_path=r["folder_path"],
            summary_status=r["summary_status"],
            summary_updated_at=_iso(r["summary_updated_at"]),
            summary_result=r["summary_result"] if with_result and "summary_result" in r.keys() else None,
        )


def _snippet(text: str, query: str, context: int = 200) -> str:
    index = text.lower().find(query.lower())
    if index < 0:
        return text[: context * 2]
    start = max(0, index - context)
    end = min(len(text), index + len(query) + context)
    prefix = "…" if start else ""
    suffix = "…" if end < len(text) else ""
    return f"{prefix}{text[start:end]}{suffix}"
