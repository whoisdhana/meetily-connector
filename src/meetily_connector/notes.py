"""Local markdown notes archive: one file per processed meeting.

Written best-effort right after a meeting is enqueued for delivery — a notes
failure never blocks or fails the webhook path. The summary comes from a
Gemini generateContent call (cheap flash model, same API key as transcription);
if that fails, the file is still written with the transcript and a
"summary unavailable" marker.

Filenames are deterministic (<YYYY-MM-DD>-<HHMM>-<slug>.md, local time) so
re-processing the same meeting overwrites its own file; a different meeting
landing on the same name gets a short id suffix instead.
"""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path

from .config import Config
from .db import MeetingRow, parse_meetily_datetime
from .gemini_transcribe import API_BASE, TranscribeError, _request
from .logging_setup import log_event
from .secrets import SecretError, resolve_secret


class SummaryError(Exception):
    """Summary generation failed; the notes file is written without it."""


SUMMARY_UNAVAILABLE = "_Summary unavailable (generation failed; transcript below)._"

_SUMMARY_PROMPT = """\
You are writing meeting notes from a transcript. The transcript is Tanglish
(Tamil-English code-mixed); English technical terms often appear transliterated
into Tamil script (e.g. வெப்சைட் = website, பிரைசிங் = pricing, டிசைன் = design).
That is normal — mentally convert such words back to English.

Write the notes in {language}. Output EXACTLY two markdown sections and
nothing else:

## Summary
- 3-10 bullets: topics discussed, decisions made, blockers, deadlines.

## Action items
- one bullet per item: owner — what — due date (absolute, Asia/Kolkata) if
  stated, else "no date". If there are none, write "- none".

Transcript:
{transcript}
"""


def slugify(title: str, max_len: int = 60) -> str:
    slug = re.sub(r"[^a-z0-9]+", "-", title.lower()).strip("-")
    return slug[:max_len].rstrip("-") or "untitled"


def write_notes(cfg: Config, meeting: MeetingRow, transcript: str,
                duration: float, *, source: str, model: str | None,
                diarized: bool) -> Path | None:
    """Exception-proof entry point. Returns the path written, or None."""
    try:
        notes_dir = Path(cfg.notes_dir).expanduser()
        notes_dir.mkdir(parents=True, exist_ok=True)

        summary_md: str | None = None
        try:
            api_key = resolve_secret(
                cfg.gemini_key_source,
                env_var=cfg.gemini_key_env,
                keychain_service=cfg.gemini_key_keychain_service,
                keychain_account=cfg.gemini_key_keychain_account,
                file_path=cfg.gemini_key_file,
            )
            summary_md = _summarize(transcript, api_key,
                                    model=cfg.notes_summary_model,
                                    language=cfg.notes_language)
        except SecretError as exc:
            log_event("notes.summary_key_unavailable", error=str(exc))
        except SummaryError as exc:
            log_event("notes.summary_failed", meeting_id=meeting.id,
                      error=str(exc))

        path = _target_path(notes_dir, meeting)
        content = _render(meeting, transcript, duration, source=source,
                          model=model, diarized=diarized, summary_md=summary_md)
        path.write_text(content, encoding="utf-8")
        log_event("notes.written", meeting_id=meeting.id, path=str(path))
        return path
    except Exception as exc:
        log_event("notes.write_failed", meeting_id=meeting.id,
                  error=type(exc).__name__)
        return None


def _summarize(transcript: str, api_key: str, *, model: str,
               language: str, timeout: int = 120) -> str:
    prompt = _SUMMARY_PROMPT.format(language=language, transcript=transcript)
    payload = {"contents": [{"parts": [{"text": prompt}]}]}
    try:
        body, _ = _request(
            f"{API_BASE}/v1beta/models/{model}:generateContent", api_key,
            headers={"Content-Type": "application/json"},
            data=json.dumps(payload).encode(), timeout=timeout,
        )
    except TranscribeError as exc:
        raise SummaryError(str(exc)) from exc
    candidates = body.get("candidates") or []
    parts = (candidates[0].get("content", {}).get("parts", [])
             if candidates else [])
    text = "\n".join(p.get("text", "") for p in parts if p.get("text"))
    if not text.strip():
        raise SummaryError("empty summary in API response")
    return text.strip()


def _target_path(notes_dir: Path, meeting: MeetingRow) -> Path:
    created = parse_meetily_datetime(meeting.created_at)
    local = created.astimezone() if created else datetime.now().astimezone()
    stem = f"{local.strftime('%Y-%m-%d-%H%M')}-{slugify(meeting.title)}"
    path = notes_dir / f"{stem}.md"
    if path.exists() and f"meeting_id: {meeting.id}" not in path.read_text(
            encoding="utf-8")[:500]:
        short_id = meeting.id.removeprefix("meeting-")[:8]
        path = notes_dir / f"{stem}-{short_id}.md"
    return path


def _render(meeting: MeetingRow, transcript: str, duration: float, *,
            source: str, model: str | None, diarized: bool,
            summary_md: str | None) -> str:
    created = parse_meetily_datetime(meeting.created_at)
    local = created.astimezone() if created else None
    front = [
        "---",
        f"meeting_id: {meeting.id}",
        f"title: {json.dumps(meeting.title, ensure_ascii=False)}",
        f"date: {local.isoformat(timespec='seconds') if local else 'unknown'}",
        f"duration_seconds: {round(duration)}",
        f"transcription_source: {source}",
        f"transcription_model: {model or 'local'}",
        f"diarized: {str(diarized).lower()}",
        "---",
        "",
        f"# {meeting.title}",
        "",
        "",
    ]
    body = summary_md if summary_md else f"## Summary\n\n{SUMMARY_UNAVAILABLE}"
    return "\n".join(front) + body + f"\n\n## Transcript\n\n{transcript}\n"
