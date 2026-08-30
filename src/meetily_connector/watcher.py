"""Watch mode: detect completed meetings, build payloads, deliver via outbox.

Trigger: summary_processes.status = 'completed' (exact upstream literal) for a
meeting not yet in sent_log/outbox, with a settle delay so summary-regeneration
flapping and retranscription races don't double-fire.
"""

from __future__ import annotations

import json
import time
from datetime import datetime, timedelta, timezone

from . import __version__
from .config import Config, ConfigError
from .db import DatabaseBusy, MeetilyDB, MeetingRow, parse_meetily_datetime
from .logging_setup import log_event
from .outbox import StateStore
from .secrets import SecretError, resolve_secret
from .webhook import deliver

TRUNCATION_MARKER = "\n[TRANSCRIPT TRUNCATED]"
# Payload budget headroom for everything besides the transcript text.
_ENVELOPE_HEADROOM = 20_000


def build_payload(cfg: Config, meeting: MeetingRow,
                  transcript: str, duration: float) -> dict:
    budget = max(cfg.max_payload_bytes - _ENVELOPE_HEADROOM, 10_000)
    original_chars = len(transcript)
    truncated = False
    encoded = transcript.encode("utf-8")
    if len(encoded) > budget:
        # UTF-8-safe cut: decode with replacement disabled by trimming to a
        # character boundary via errors="ignore".
        transcript = encoded[:budget].decode("utf-8", errors="ignore")
        transcript += TRUNCATION_MARKER
        truncated = True

    summary_result = None
    if meeting.summary_result:
        try:
            summary_result = json.loads(meeting.summary_result)
        except ValueError:
            summary_result = None

    return {
        "event": "meeting.completed",
        "version": 1,
        "meeting": {
            "id": meeting.id,
            "title": meeting.title,
            "created_at": meeting.created_at,
            "duration_seconds": round(duration),
            "language_hint": cfg.language_hint or None,
            "folder_path": meeting.folder_path,
        },
        "transcript": {
            "text": transcript,
            "truncated": truncated,
            "original_chars": original_chars,
        },
        "meetily_summary": {
            "status": meeting.summary_status,
            "result": summary_result,
        },
        "connector": {
            "name": "meetily-connector",
            "version": __version__,
            "sent_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        },
    }


def _settled(meeting: MeetingRow, settle_seconds: int) -> bool:
    updated = parse_meetily_datetime(meeting.summary_updated_at)
    if updated is None:
        return True  # unparseable timestamp: don't wedge the meeting forever
    return datetime.now(timezone.utc) - updated >= timedelta(seconds=settle_seconds)


def tick(cfg: Config, store: StateStore, dry_run: bool = False) -> int:
    """One poll cycle. Returns number of meetings newly detected."""
    try:
        db_path = cfg.resolve_db_path()
    except ConfigError:
        # Meetily not installed or DB not created yet: keep polling quietly
        # rather than crash-looping under launchd.
        log_event("tick.no_database")
        return 0
    detected = 0
    try:
        with MeetilyDB(db_path) as db:
            candidates = db.completed_unsent(store.seen_meeting_ids())
            for meeting in candidates:
                if not _settled(meeting, cfg.settle_seconds):
                    continue  # re-examined next tick
                transcript, duration = db.get_transcript_text(meeting.id)
                if duration < cfg.min_duration_seconds or not transcript.strip():
                    if not dry_run:
                        store.mark_skipped_trivial(meeting.id)
                    continue
                payload = build_payload(cfg, meeting, transcript, duration)
                if dry_run:
                    print(json.dumps(payload, ensure_ascii=False, indent=2))
                else:
                    store.enqueue(meeting.id, payload)
                detected += 1
    except DatabaseBusy:
        log_event("tick.skipped_db_busy")
        return 0

    if not dry_run:
        _drain_outbox(cfg, store)
    return detected


def _drain_outbox(cfg: Config, store: StateStore) -> None:
    due = store.due()
    if not due:
        return
    try:
        secret = resolve_secret(
            cfg.secret_source,
            env_var=cfg.secret_env,
            keychain_service=cfg.secret_keychain_service,
            keychain_account=cfg.secret_keychain_account,
            file_path=cfg.secret_file,
        )
    except SecretError as exc:
        # Config problem, not per-meeting: log once per drain, retry next tick.
        log_event("outbox.secret_unavailable", error=str(exc))
        return
    for meeting_id, payload, attempts in due:
        result = deliver(cfg.webhook_url, secret, payload,
                         cfg.allow_insecure_transport)
        if result.ok:
            store.record_success(meeting_id, result.http_status or 0,
                                 result.payload_sha256)
        else:
            store.record_failure(meeting_id, attempts, result.error,
                                 result.http_status)


def run_watch(cfg: Config, once: bool = False, dry_run: bool = False) -> int:
    if not dry_run:
        cfg.validate_webhook_url()
    store = StateStore(cfg)
    log_event("watch.started", once=once, dry_run=dry_run,
              poll_seconds=cfg.poll_interval_seconds)
    try:
        while True:
            detected = tick(cfg, store, dry_run=dry_run)
            if detected:
                log_event("tick.detected", count=detected)
            if once:
                if dry_run and not detected:
                    print("(no new completed meetings)", flush=True)
                return 0
            time.sleep(cfg.poll_interval_seconds)
    finally:
        store.close()
