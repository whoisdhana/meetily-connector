# Meetily schema notes — verified against upstream v0.4.0

Verified 2026-08-30 from a clone of `Zackriya-Solutions/meeting-minutes` at
`frontend/src-tauri` (tauri.conf.json version 0.4.0, identifier `com.meetily.ai`).
The `backend/` Python tree is archived; the Rust/Tauri core is authoritative.

## Database location (macOS)

```
~/Library/Application Support/com.meetily.ai/meeting_minutes.sqlite       <- ACTIVE
~/Library/Application Support/com.meetily.ai/meeting_minutes.sqlite-wal
~/Library/Application Support/com.meetily.ai/meeting_minutes.sqlite-shm
~/Library/Application Support/com.meetily.ai/meeting_minutes.db           <- legacy one-time import source only
```

Filename is `meeting_minutes.sqlite`, NOT `.db` (`src/database/manager.rs:44-63`).
Flat in the app data dir, no subdirectory. WAL mode; the app only checkpoints on
graceful shutdown — readers must honour the `-wal` sidecar (normal sqlite3 open
does; never copy the `.sqlite` file alone).

## summary_processes.status literals

No enum — hardcoded SQL literals in `src/database/repositories/summary.rs`:

| Literal | Meaning | Line |
|---|---|---|
| `PENDING` (UPPERCASE) | created/reset; also the state during the whole LLM run — there is no `processing` | 97 |
| `completed` (lowercase) | success | 135 |
| `failed` (lowercase) | failure (prior result restored from backup) | 166 |
| `cancelled` (lowercase) | user cancel (prior result restored) | 200 |

The summary JSON lives in `summary_processes.result` (TEXT). A meeting can
oscillate `completed → PENDING → completed/failed/cancelled` on regeneration.
A failed status write can leave a run stuck at `PENDING` forever.

## Timestamps — two incompatible encodings

- `meetings.created_at/updated_at`, `summary_processes.*_time/created_at/updated_at`,
  `transcript_chunks.created_at`: sqlx-encoded `DateTime<Utc>` —
  **`2026-08-30 14:23:05.123456789+00:00`** (space separator, UTC offset, no `T`).
- `transcripts.timestamp`: a bare **`HH:MM:SS`** clock string (effectively UTC),
  no date — unusable for ordering. Use `audio_start_time` (REAL, seconds from
  recording start) instead.

## Cardinality

- `transcripts`: **N rows per meeting** (one per segment); order by
  `audio_start_time`. The `summary`/`action_items`/`key_points` columns are
  **dead — never written**, always NULL (vestigial from the archived backend).
  `speaker` (added 20251110000001) is also never written currently.
- `transcript_chunks`: **exactly 1 row per meeting** (`meeting_id` PK) — the full
  concatenated text snapshot taken when a summary was requested, plus LLM params.
  Only exists if a summary was ever generated.

## Write ordering

Transcript segments are saved in a single all-or-nothing transaction *before*
a summary process row can exist (FK on meetings.id). So `status='completed'`
implies the transcript is committed. Caveats:
- **Retranscription** (`audio/retranscription.rs:436`) deletes + rewrites
  `transcripts` without resetting `completed` — a summary can describe stale text.
- Keep a settle delay (`summary updated_at < now - settle_seconds`) to sidestep
  regeneration flapping.

## Relevant tables (effective shape after 10 migrations)

```sql
meetings(id TEXT PK, title, created_at, updated_at, folder_path)
transcripts(id TEXT PK, meeting_id FK, transcript, timestamp, summary*, action_items*,
            key_points*, audio_start_time REAL, audio_end_time REAL, duration REAL,
            speaker*)                                    -- * = never written
summary_processes(meeting_id PK, status, created_at, updated_at, error, result,
                  start_time, end_time, chunk_count, processing_time, metadata,
                  result_backup, result_backup_timestamp)
transcript_chunks(meeting_id PK, meeting_name, transcript_text, model, model_name,
                  chunk_size, overlap, created_at)
meeting_notes(meeting_id PK, notes_markdown, notes_json, created_at, updated_at)
settings(id PK, provider, model, whisperModel, …ApiKey columns in PLAINTEXT,
         ollamaEndpoint, customOpenAIConfig, geminiApiKey)
```

No FTS tables anywhere; upstream search is a leading-wildcard `LIKE` scan.
Only index in the schema: `meeting_notes(meeting_id)` — notably none on
`transcripts.meeting_id`.
