import sqlite3

import pytest

from meetily_connector.db import MeetilyDB, parse_meetily_datetime


def test_readonly_enforced(fixture_db):
    with MeetilyDB(fixture_db) as db:
        with pytest.raises(sqlite3.OperationalError):
            db.conn.execute("INSERT INTO meetings (id, title, created_at, updated_at) "
                            "VALUES ('x', 'x', 'now', 'now')")


def test_parse_sqlx_datetime():
    # upstream sqlx format: space separator, fractional seconds, +00:00 offset
    dt = parse_meetily_datetime("2026-08-30 14:23:05.123456+00:00")
    assert dt is not None and dt.utcoffset().total_seconds() == 0
    # naive legacy rows are reinterpreted as UTC (matches upstream DateTimeUtc)
    naive = parse_meetily_datetime("2026-08-30 14:23:05")
    assert naive is not None and naive.tzinfo is not None
    assert parse_meetily_datetime(None) is None
    assert parse_meetily_datetime("garbage") is None


def test_completed_unsent_selects_exactly_ready_meetings(fixture_db):
    with MeetilyDB(fixture_db) as db:
        ids = {m.id for m in db.completed_unsent(set())}
    # PENDING, failed, and no-summary meetings excluded; all completed included
    # (settle/trivial filtering happens in the watcher, not the query).
    assert ids == {"meeting-done", "meeting-trivial", "meeting-fresh", "meeting-huge"}


def test_completed_unsent_respects_seen_set(fixture_db):
    with MeetilyDB(fixture_db) as db:
        ids = {m.id for m in db.completed_unsent({"meeting-done", "meeting-huge"})}
    assert "meeting-done" not in ids and "meeting-huge" not in ids


def test_transcript_ordered_and_duration(fixture_db):
    with MeetilyDB(fixture_db) as db:
        text, duration = db.get_transcript_text("meeting-done")
    assert text.count("deployment") == 6
    assert duration == 180.0  # 6 segments * 30s, from max(audio_end_time)


def test_search(fixture_db):
    with MeetilyDB(fixture_db) as db:
        hits = db.search_meetings("deployment")
    assert [h["id"] for h in hits] == ["meeting-done"]
    assert "deployment" in hits[0]["snippet"]


def test_locked_db_raises_databasebusy(fixture_db):
    writer = sqlite3.connect(fixture_db)
    writer.execute("BEGIN EXCLUSIVE")
    try:
        from meetily_connector.db import DatabaseBusy

        with MeetilyDB(fixture_db) as db:
            with pytest.raises(DatabaseBusy):
                db.list_meetings()
    finally:
        writer.rollback()
        writer.close()


def test_schema_warnings_detect_drift(tmp_path):
    path = tmp_path / "drifted.sqlite"
    conn = sqlite3.connect(path)
    conn.execute("CREATE TABLE meetings (id TEXT PRIMARY KEY, title TEXT)")
    conn.commit()
    conn.close()
    with MeetilyDB(path) as db:
        warnings = db.schema_warnings()
    assert any("meetings" in w for w in warnings)
    assert any("transcripts" in w for w in warnings)
