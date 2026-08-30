import json

from meetily_connector.db import MeetilyDB
from meetily_connector.outbox import StateStore
from meetily_connector.watcher import TRUNCATION_MARKER, build_payload, tick


def test_tick_enqueues_ready_skips_trivial_and_fresh(cfg):
    store = StateStore(cfg)
    try:
        detected = tick(cfg, store)
        # meeting-done and meeting-huge enqueue; meeting-trivial (40s) is
        # skipped; meeting-fresh fails the 30s settle delay (summary 5s old).
        assert detected == 2
        queued = {m for m, _, _ in store.due()}
        assert queued == {"meeting-done", "meeting-huge"}
        seen = store.seen_meeting_ids()
        assert "meeting-trivial" in seen  # skipped_trivial recorded
        assert "meeting-fresh" not in seen  # will be re-examined next tick
    finally:
        store.close()


def test_tick_is_idempotent(cfg):
    store = StateStore(cfg)
    try:
        assert tick(cfg, store) == 2
        assert tick(cfg, store) == 0  # everything already queued or skipped
    finally:
        store.close()


def test_payload_shape_and_utf8_safe_truncation(cfg):
    cfg.max_payload_bytes = 30_000
    cfg.language_hint = "ta-en-mixed"
    with MeetilyDB(cfg.resolve_db_path()) as db:
        meeting = db.get_meeting("meeting-huge")
        text, duration = db.get_transcript_text("meeting-huge")
    payload = build_payload(cfg, meeting, text, duration)

    assert payload["event"] == "meeting.completed"
    assert payload["meeting"]["language_hint"] == "ta-en-mixed"
    assert payload["transcript"]["truncated"] is True
    assert payload["transcript"]["text"].endswith(TRUNCATION_MARKER)
    assert payload["transcript"]["original_chars"] == len(text)
    # Multi-byte Tamil text at the cut point must not produce a broken char.
    payload["transcript"]["text"].encode("utf-8")  # would raise on lone surrogate
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    assert len(body) <= cfg.max_payload_bytes


def test_payload_passes_meetily_summary_through(cfg):
    with MeetilyDB(cfg.resolve_db_path()) as db:
        meeting = db.get_meeting("meeting-done")
        text, duration = db.get_transcript_text("meeting-done")
    payload = build_payload(cfg, meeting, text, duration)
    assert payload["meetily_summary"]["status"] == "completed"
    assert payload["meetily_summary"]["result"]["summary"] == "Sprint planning recap"
    assert payload["meeting"]["duration_seconds"] == 180
    # created_at is normalized tz-aware ISO
    assert "+00:00" in payload["meeting"]["created_at"]


def test_dry_run_writes_no_state(cfg, capsys):
    store = StateStore(cfg)
    try:
        detected = tick(cfg, store, dry_run=True)
        assert detected == 2
        assert store.depth() == 0
        assert store.seen_meeting_ids() == set()
        out = capsys.readouterr().out
        assert '"meeting.completed"' in out
    finally:
        store.close()
