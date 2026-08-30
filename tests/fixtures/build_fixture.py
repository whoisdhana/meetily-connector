"""Build a Meetily-shaped fixture DB for tests.

Timestamps match upstream's sqlx encoding: "YYYY-MM-DD HH:MM:SS.ffffff+00:00"
(space separator). Status literals match summary.rs: 'PENDING' upper,
'completed'/'failed'/'cancelled' lower.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

SCHEMA = (Path(__file__).parent / "schema.sql").read_text(encoding="utf-8")

TANGLISH = (
    "okay so deployment na next friday ku pannalam nu solliten. "
    "Ravi anna neenga staging server la config update pannunga. "
    "aprum client demo ku slides ready panna vendiyathu Priya. "
    "database migration script test aagala innum, adhu urgent."
)


def _sqlx_ts(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S.%f") + "+00:00"


def build(path: Path, now: datetime | None = None) -> None:
    # Default to the real clock: the watcher's settle-delay compares summary
    # updated_at against real now, so fixture times must be relative to it.
    now = now or datetime.now(timezone.utc)
    if path.exists():
        path.unlink()
    conn = sqlite3.connect(path)
    conn.executescript(SCHEMA)

    def add_meeting(mid: str, title: str, created: datetime,
                    segments: list[str], seg_seconds: float = 30.0) -> None:
        conn.execute(
            "INSERT INTO meetings (id, title, created_at, updated_at, folder_path) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, title, _sqlx_ts(created), _sqlx_ts(created), f"/tmp/recordings/{mid}"),
        )
        for i, text in enumerate(segments):
            conn.execute(
                "INSERT INTO transcripts (id, meeting_id, transcript, timestamp, "
                "audio_start_time, audio_end_time, duration) VALUES (?, ?, ?, ?, ?, ?, ?)",
                (f"transcript-{mid}-{i}", mid, text, "12:00:00",
                 i * seg_seconds, (i + 1) * seg_seconds, seg_seconds),
            )

    def add_summary(mid: str, status: str, updated: datetime,
                    result: dict | None = None) -> None:
        conn.execute(
            "INSERT INTO summary_processes (meeting_id, status, created_at, updated_at, result) "
            "VALUES (?, ?, ?, ?, ?)",
            (mid, status, _sqlx_ts(updated), _sqlx_ts(updated),
             json.dumps(result) if result else None),
        )

    # 1. Completed meeting, ready to send.
    add_meeting("meeting-done", "Sprint planning (Tanglish)",
                now - timedelta(hours=2), [TANGLISH] * 6)
    add_summary("meeting-done", "completed", now - timedelta(hours=1),
                {"summary": "Sprint planning recap", "action_items": ["ship it"]})

    # 2. Still pending (uppercase literal, as upstream writes it).
    add_meeting("meeting-pending", "Standup", now - timedelta(minutes=30),
                ["quick standup notes"] * 4)
    add_summary("meeting-pending", "PENDING", now - timedelta(minutes=10))

    # 3. Failed summary.
    add_meeting("meeting-failed", "1:1", now - timedelta(days=1), ["one on one"] * 4)
    add_summary("meeting-failed", "failed", now - timedelta(days=1))

    # 4. Trivial: completed but only 40s of audio.
    add_meeting("meeting-trivial", "Accidental recording",
                now - timedelta(hours=3), ["oops"], seg_seconds=40.0)
    add_summary("meeting-trivial", "completed", now - timedelta(hours=3))

    # 5. Completed but summary updated seconds ago (settle-delay case).
    add_meeting("meeting-fresh", "Just finished", now - timedelta(minutes=20),
                ["fresh meeting content"] * 5)
    add_summary("meeting-fresh", "completed", now - timedelta(seconds=5))

    # 6. Huge transcript for truncation tests (multi-byte safe: Tamil text).
    add_meeting("meeting-huge", "All hands", now - timedelta(days=2),
                ["வணக்கம் everyone, " + "x" * 5000] * 40)
    add_summary("meeting-huge", "completed", now - timedelta(days=2))

    # 7. No summary row at all.
    add_meeting("meeting-nosummary", "Unprocessed", now - timedelta(days=3),
                ["never summarized"] * 3)

    conn.commit()
    conn.close()


if __name__ == "__main__":
    import sys

    out = Path(sys.argv[1] if len(sys.argv) > 1 else "fixture.sqlite")
    build(out)
    print(f"built {out}")
