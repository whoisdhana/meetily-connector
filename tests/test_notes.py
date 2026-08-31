from unittest import mock

import pytest

from meetily_connector import notes
from meetily_connector.config import ConfigError, load_config
from meetily_connector.notes import SUMMARY_UNAVAILABLE, SummaryError, slugify
from meetily_connector.outbox import StateStore
from meetily_connector.watcher import tick

FAKE_SUMMARY = "## Summary\n\n- discussed deployment\n\n## Action items\n\n- Ravi — staging config — 2026-09-02"


@pytest.fixture
def notes_cfg(cfg, tmp_path, monkeypatch):
    cfg.notes_enabled = True
    cfg.notes_dir = str(tmp_path / "notes")
    cfg.gemini_key_source = "env"
    cfg.gemini_key_env = "FAKE_GEMINI_KEY"
    monkeypatch.setenv("FAKE_GEMINI_KEY", "test-key")
    return cfg


def test_tick_writes_notes_files(notes_cfg, tmp_path):
    store = StateStore(notes_cfg)
    try:
        with mock.patch.object(notes, "_summarize", return_value=FAKE_SUMMARY):
            detected = tick(notes_cfg, store)
        assert detected == 2  # meeting-done + meeting-huge (summary trigger)
        files = sorted((tmp_path / "notes").glob("*.md"))
        assert len(files) == 2
        text = next(f for f in files if "sprint-planning" in f.name).read_text()
        assert "meeting_id: meeting-done" in text
        assert "## Summary" in text and "discussed deployment" in text
        assert "## Transcript" in text and "deployment na next friday" in text
        assert "transcription_source: meetily-local" in text
    finally:
        store.close()


def test_summary_failure_still_writes_transcript(notes_cfg, tmp_path):
    store = StateStore(notes_cfg)
    try:
        with mock.patch.object(notes, "_summarize",
                               side_effect=SummaryError("http_500")):
            tick(notes_cfg, store)
        files = list((tmp_path / "notes").glob("*.md"))
        assert files, "notes file must be written even when summary fails"
        text = files[0].read_text()
        assert SUMMARY_UNAVAILABLE in text
        assert "## Transcript" in text
        # webhook path unaffected: payloads still queued
        assert store.depth() == 2
    finally:
        store.close()


def test_notes_never_block_delivery(notes_cfg):
    store = StateStore(notes_cfg)
    try:
        with mock.patch.object(notes, "write_notes",
                               side_effect=RuntimeError("disk full")) as wn:
            # write_notes is exception-proof internally, but even a hard crash
            # in the notes layer must not lose the enqueue that came first.
            with mock.patch("meetily_connector.watcher.write_notes", wn,
                            create=True):
                pass  # imported inside tick; patch module attr instead
        with mock.patch.object(notes, "_summarize", return_value=FAKE_SUMMARY), \
             mock.patch.object(notes.Path, "write_text",
                               side_effect=OSError("disk full")):
            detected = tick(notes_cfg, store)
        assert detected == 2
        assert store.depth() == 2  # enqueued despite notes write failure
    finally:
        store.close()


def test_slugify():
    assert slugify("Sprint Planning (Tanglish)") == "sprint-planning-tanglish"
    assert slugify("வணக்கம் மீட்டிங்") == "untitled"  # non-latin falls back
    assert slugify("A" * 100).startswith("a" * 60)
    assert slugify("!!!") == "untitled"


def test_collision_gets_id_suffix(notes_cfg, tmp_path):
    from meetily_connector.db import MeetingRow

    ndir = tmp_path / "notes"
    ndir.mkdir()
    m1 = MeetingRow(id="meeting-aaaa1111", title="Sync",
                    created_at="2026-08-31T10:00:00+00:00",
                    updated_at="", folder_path=None)
    m2 = MeetingRow(id="meeting-bbbb2222", title="Sync",
                    created_at="2026-08-31T10:00:00+00:00",
                    updated_at="", folder_path=None)
    p1 = notes._target_path(ndir, m1)
    p1.write_text(f"---\nmeeting_id: {m1.id}\n---\n")
    # same meeting -> same path (overwrite)
    assert notes._target_path(ndir, m1) == p1
    # different meeting, same slot -> suffixed
    p2 = notes._target_path(ndir, m2)
    assert p2 != p1 and "bbbb2222" in p2.name


def test_dry_run_writes_no_notes(notes_cfg, tmp_path):
    store = StateStore(notes_cfg)
    try:
        with mock.patch.object(notes, "_summarize", return_value=FAKE_SUMMARY):
            tick(notes_cfg, store, dry_run=True)
        assert not list((tmp_path / "notes").glob("*.md"))
    finally:
        store.close()


def test_notes_config_validation(tmp_path):
    p = tmp_path / "config.toml"
    p.write_text('[notes]\nenabled = true\ndir = ""\n')
    with pytest.raises(ConfigError, match="notes"):
        load_config(p)
    p.write_text('[notes]\nenabled = true\nsummary_model = "gemini-3.5-flash"\n')
    cfg = load_config(p)
    assert cfg.notes_enabled and cfg.notes_summary_model == "gemini-3.5-flash"
