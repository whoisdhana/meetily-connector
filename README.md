# meetily-connector

Bridge [Meetily](https://github.com/Zackriya-Solutions/meeting-minutes) meeting
transcripts and summaries to any AI agent — with zero runtime dependencies.

Meetily records and transcribes meetings entirely locally. This connector reads
its SQLite database (strictly read-only) and exposes the data two ways:

| Mode | What it does | Who it's for |
|---|---|---|
| `meetily-mcp serve` | stdio MCP server: `list_meetings`, `get_transcript`, `search_meetings`, … | Any MCP-capable agent (Claude, Hermes, …) that should browse meetings on demand |
| `meetily-mcp watch` | Daemon: when a meeting's summary completes, POST a signed `meeting.completed` JSON payload to a webhook you configure | Automations that should react to every finished meeting without being asked |

## Install

Python 3.11+, no dependencies:

```sh
pip install meetily-connector      # or: pipx install meetily-connector
meetily-mcp init                   # writes ~/.meetily-connector/config.toml
```

## MCP server

Add to any MCP client config:

```json
{ "mcpServers": { "meetily": { "command": "meetily-mcp", "args": ["serve"] } } }
```

Tools: `list_meetings`, `get_meeting`, `get_transcript` (paged), `search_meetings`,
`get_summary`, `connector_status`. All read-only.

## Watcher

Configure `[watch] webhook_url` and a signing secret source (env var, macOS
Keychain, or a chmod-600 file — secrets never live in the config), then:

```sh
meetily-mcp watch --once --dry-run   # print the payload, send nothing
meetily-mcp watch                    # run the loop
meetily-mcp init --launchd           # render a launchd plist for macOS autostart
```

Behavior:

- Two trigger modes: `trigger = "summary"` fires when Meetily's own summary
  generation reports **completed**; `trigger = "transcript"` fires as soon as
  the recording stops and the transcript is saved — no Meetily summary model
  needed (useful when the receiver does its own LLM pass anyway).
- Optional cloud transcription: `transcriber = "gemini"` re-transcribes the
  recording with Google Gemini Transcribe (speaker diarization, 85+ languages,
  strong on code-mixed speech) and ships that instead of the local transcript,
  falling back to local on any API failure. Audio is uploaded to Google for
  this — the file is deleted from Google's storage right after transcription.
- Skips trivial recordings (`min_duration_seconds`).
- Durable outbox: if the receiver is down, payloads queue in the connector's own
  SQLite and retry with exponential backoff — forever by default for transient
  failures, bounded for permanent 4xx errors.
- Idempotent: each meeting is delivered at most once (`replay <id>` to re-send).
- Payloads are HMAC-SHA256 signed (`X-Webhook-Signature-V2` over
  `"<timestamp>.<body>"`).

### Payload (`meeting.completed` v1)

```json
{
  "event": "meeting.completed",
  "version": 1,
  "meeting": {"id": "…", "title": "…", "created_at": "…", "duration_seconds": 1840,
               "language_hint": "ta-en-mixed"},
  "transcript": {"text": "…", "truncated": false, "original_chars": 48210},
  "meetily_summary": {"status": "completed", "result": {}},
  "connector": {"name": "meetily-connector", "version": "0.1.0", "sent_at": "…"}
}
```

## Security posture

- Meetily's DB is opened read-only (`mode=ro` + `PRAGMA query_only`); connector
  state lives in `~/.meetily-connector/` (0700/0600).
- Secrets come from env / Keychain / 0600 files only, and are never logged.
- Transcript text never appears in logs — only ids, sizes, and statuses.
- Plain `http://` is refused for non-loopback hosts unless you explicitly set
  `allow_insecure_transport = true`.

## Example deployment: Hermes agent

If you run a [Hermes](https://github.com/whoisdhana) agent, the watcher pairs
with Hermes' generic webhook adapter: enable the adapter on loopback, subscribe
a route whose prompt cleans up the transcript and extracts action items, and
point `webhook_url` at `http://127.0.0.1:8644/webhooks/meetily-meeting`. Full
walkthrough in [`deploy/hermes-webhook-subscription.md`](deploy/hermes-webhook-subscription.md).

## License

MIT
