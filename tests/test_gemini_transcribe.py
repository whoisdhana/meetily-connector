from pathlib import Path
from unittest import mock

import pytest

from meetily_connector.gemini_transcribe import (
    GeminiTranscript,
    TranscribeError,
    _extract_text,
    find_audio_file,
)
from meetily_connector.watcher import _cloud_transcript, build_payload


def test_extract_prefers_speaker_labels():
    response = {"interaction": {
        "output_text": "hello world how are you",
        "steps": [{"content": [{
            "type": "text", "text": "hello world how are you",
            "annotations": [
                {"type": "word_info", "text": "hello", "speaker": "spk_1"},
                {"type": "word_info", "text": "world", "speaker": "spk_1"},
                {"type": "word_info", "text": "how", "speaker": "spk_2"},
                {"type": "word_info", "text": "are", "speaker": "spk_2"},
                {"type": "word_info", "text": "you", "speaker": "spk_2"},
            ],
        }]}],
    }}
    assert _extract_text(response) == "spk_1: hello world\nspk_2: how are you"


def test_extract_falls_back_to_output_text():
    assert _extract_text({"output_text": "plain transcript"}) == "plain transcript"
    assert _extract_text({"interaction": {"steps": [{"content": [
        {"type": "text", "text": "from steps"}]}]}}) == "from steps"


def test_find_audio_file_picks_largest(tmp_path):
    (tmp_path / "clip.wav").write_bytes(b"x" * 10)
    (tmp_path / "full.wav").write_bytes(b"x" * 1000)
    (tmp_path / "notes.txt").write_text("not audio")
    assert find_audio_file(str(tmp_path)).name == "full.wav"
    assert find_audio_file(str(tmp_path / "missing")) is None


def test_cloud_transcript_fallback_on_api_failure(cfg, tmp_path, monkeypatch):
    cfg.transcriber = "gemini"
    cfg.gemini_key_source = "env"
    cfg.gemini_key_env = "FAKE_GEMINI_KEY"
    monkeypatch.setenv("FAKE_GEMINI_KEY", "test-key")
    (tmp_path / "rec").mkdir()
    (tmp_path / "rec" / "audio.wav").write_bytes(b"RIFF")

    from meetily_connector.db import MeetingRow

    meeting = MeetingRow(id="m1", title="t", created_at="", updated_at="",
                         folder_path=str(tmp_path / "rec"))
    with mock.patch("meetily_connector.gemini_transcribe.transcribe_file",
                    side_effect=TranscribeError("http_500")):
        assert _cloud_transcript(cfg, meeting, 600.0) is None  # -> local fallback


def test_cloud_transcript_success_and_diarization_cap(cfg, tmp_path, monkeypatch):
    cfg.transcriber = "gemini"
    cfg.gemini_key_source = "env"
    cfg.gemini_key_env = "FAKE_GEMINI_KEY"
    monkeypatch.setenv("FAKE_GEMINI_KEY", "test-key")
    (tmp_path / "rec").mkdir()
    (tmp_path / "rec" / "audio.wav").write_bytes(b"RIFF")

    from meetily_connector.db import MeetingRow

    meeting = MeetingRow(id="m1", title="t", created_at="2026-08-31T10:00:00+00:00",
                         updated_at="", folder_path=str(tmp_path / "rec"))
    captured = {}

    def fake_transcribe(path, key, *, model, language_codes, diarization,
                        custom_vocabulary):
        captured["diarization"] = diarization
        return GeminiTranscript(text="spk_1: வணக்கம் team", model=model,
                                diarized=diarization)

    with mock.patch("meetily_connector.gemini_transcribe.transcribe_file",
                    fake_transcribe):
        result = _cloud_transcript(cfg, meeting, 600.0)
        assert result.diarized is True
        long_result = _cloud_transcript(cfg, meeting, 2400.0)  # 40 min
        assert long_result.diarized is False  # over the 30-min diarization cap

    payload = build_payload(cfg, meeting, result.text, 600.0,
                            transcription_source="gemini",
                            transcription_model=result.model,
                            diarized=result.diarized)
    assert payload["transcript"]["source"] == "gemini"
    assert payload["transcript"]["model"] == "gemini-3.5-transcribe"
    assert payload["transcript"]["diarized"] is True


def test_transcriber_stays_local_by_default(cfg):
    from meetily_connector.db import MeetingRow

    meeting = MeetingRow(id="m1", title="t", created_at="", updated_at="",
                         folder_path="/nonexistent")
    assert _cloud_transcript(cfg, meeting, 600.0) is None
    payload = build_payload(cfg, meeting, "local text", 600.0)
    assert payload["transcript"]["source"] == "meetily-local"


def test_unsupported_extension_raises(tmp_path):
    from meetily_connector.gemini_transcribe import transcribe_file

    bad = tmp_path / "audio.xyz"
    bad.write_bytes(b"data")
    with pytest.raises(TranscribeError, match="unsupported audio extension"):
        transcribe_file(bad, "key", model="m", language_codes=[],
                        diarization=False, custom_vocabulary=[])
