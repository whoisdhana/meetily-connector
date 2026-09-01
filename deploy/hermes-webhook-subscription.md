# Example deployment: Meetily → Hermes agent → HermesNag

> **Automated:** `setup.sh` (see [SETUP.md](../SETUP.md)) performs everything
> below interactively. This document remains as the reference for what it does
> and why. The route's prompt now lives in `src/meetily_connector/route.py`
> (single source of truth); the prompt text below may drift.

This walks through wiring the watcher into a [Hermes](https://hermes.chat) agent
so that every finished meeting gets an LLM pass (transcript cleanup + action-item
extraction) and the extracted tasks land in a task store the agent can write to.
Contract details below were verified against Hermes gateway v0.20.6.

Personal-deployment specifics (hostnames, ports, profile names) belong here and
in your own config — never in the connector package.

## 0. Prerequisites

- Hermes gateway running (`launchctl print gui/$(id -u)/ai.hermes.gateway`).
- Meetily installed and at least one meeting recorded+summarized.
- `meetily-mcp watch --once --dry-run` prints a payload.

## 1. Enable the Hermes webhook adapter (loopback only)

Add to `~/.hermes/config.yaml` under the existing `gateway:` → `platforms:` block:

```yaml
gateway:
  platforms:
    webhook:
      enabled: true
      extra:
        host: "127.0.0.1"   # MANDATORY: the default (null) binds ALL interfaces
        port: 8644
```

**Config location quirk (v0.20.6):** the running gateway reads
`gateway.platforms.webhook`, but the `hermes webhook ...` CLI reads top-level
`platforms.webhook`. Add the block in BOTH places (keep host/port in sync) or
`hermes webhook subscribe` will claim the platform is disabled while the
adapter is in fact listening.

Restart and verify:

```sh
launchctl kickstart -k gui/$(id -u)/ai.hermes.gateway
grep "\[webhook\] Listening" ~/.hermes/logs/gateway.log | tail -1
# expect:  [webhook] Listening on 127.0.0.1:8644 — routes: ...
curl -s http://127.0.0.1:8644/health
# expect:  {"status": "ok", "platform": "webhook"}
```

## 2. Create the shared HMAC secret (once)

```sh
SECRET=$(python3 -c "import secrets; print(secrets.token_hex(32))")
security add-generic-password -a meetily -s meetily-webhook -w "$SECRET"
```

The connector reads it from the Keychain — in `~/.meetily-connector/config.toml`:

```toml
[watch]
webhook_url = "http://127.0.0.1:8644/webhooks/meetily-meeting"
secret_source = "keychain"
secret_keychain_service = "meetily-webhook"
secret_keychain_account = "meetily"
```

## 3. Subscribe the route

**Footguns:** (a) re-running `hermes webhook subscribe` WITHOUT `--secret`
rotates the secret and silently breaks the running watcher — always pass
`--secret` explicitly. (b) every subscribe/update REWRITES the subscription and
drops hand-edited keys (`toolsets`, `profile`) — re-add them to
`webhook_subscriptions.json` after any prompt update (step 4).

```sh
hermes webhook subscribe meetily-meeting \
  --secret "$(security find-generic-password -a meetily -s meetily-webhook -w)" \
  --description "Meetily meeting.completed ingestion" \
  --prompt "$(cat <<'EOF'
A Meetily meeting recording just finished.

Meeting: {meeting.title} (id {meeting.id}), started {meeting.created_at}, duration {meeting.duration_seconds}s.
Transcript (truncated={transcript.truncated}):
{transcript.text}

The transcript is Tanglish (Tamil-English code-mixed) from local speech
recognition and may be garbled. Do the following, in order:

1. NORMALIZE: keep English technical terms, product names, and proper nouns in
   Latin script exactly as spoken — never transliterate them into Tamil script.
   Fix obvious transcription errors only when confident.
2. SUMMARIZE in English: 5-10 bullets covering decisions, blockers, deadlines.
3. EXTRACT ACTION ITEMS: owner (person named in the meeting, else "unassigned"),
   what, and due date if stated. Resolve relative dates ("next Friday") to
   absolute dates from the meeting start time, timezone Asia/Kolkata.
4. CONFIDENCE CHECK: if the transcript is too garbled or truncated to trust,
   do NOT invent action items — instead create exactly one task titled
   "Review meeting: {meeting.title}" explaining why extraction failed.
5. CREATE TASKS: for each action item I should track, call the hermesnag
   create_task tool with: title = the action; notes = context + owner;
   source_ref = "meetily:{meeting.id}"; due_at as timezone-aware ISO-8601
   (e.g. 2026-09-05T18:00:00+05:30) or omitted; priority "normal" ("high" only
   if the meeting flagged urgency) — NEVER "must". First list existing tasks
   and skip any you already created for this source_ref (re-delivery must not
   duplicate).
6. REPLY with the English summary and the list of tasks you created. If the
   meeting had no decisions and no action items, reply [SILENT].
EOF
)"
```

Notes on the template contract (verified against the adapter):
- `{dot.notation}` walks nested JSON dicts; keys must be `[a-zA-Z0-9_.]` only
  (the connector's payload keys comply). Missing keys render as the literal
  placeholder — typos ship into the prompt, so proofread.
- Dict/list values are JSON-dumped truncated to 2000 chars; `{__raw__}` gives
  the whole payload capped at 4000. `{transcript.text}` is a string, so it is
  NOT subject to those caps.
- The route is bound to the **default profile** unless you hand-edit `profile`
  into `~/.hermes/webhook_subscriptions.json` (the CLI deliberately can't).
- Hot-reloaded per request — no gateway restart needed after subscribing.
- A valid signature proves origin, not payload safety: the transcript is
  untrusted text entering a prompt. Anything said in a meeting is potential
  prompt injection; keep the route's toolset minimal (see step 4).

## 4. Give the profile a task-creation tool (hermesnag MCP)

The Hermes agent needs an MCP server to write tasks. For HermesNag, wrap the
existing stdio server so the API token comes from the Keychain at spawn time
(never on disk):

```sh
mkdir -p ~/.hermes/bin
cat > ~/.hermes/bin/hermesnag-mcp.sh <<'EOF'
#!/bin/sh
# HermesNag MCP launcher (Mac side).
# run-server.sh expects the server box's ~/apps/hermes-nag/.env, which doesn't
# exist on the Mac — so exec server.py directly. Token comes from the login
# Keychain into process env only.
HERMESNAG_TOKEN="$(security find-generic-password -a hermesnag -s hermesnag-token -w)" || exit 1
export HERMESNAG_TOKEN
export HERMESNAG_URL="http://127.0.0.1:8787"
exec /usr/bin/env python3 "$HOME/HermesNag/hermes-tool/server.py"
EOF
chmod 700 ~/.hermes/bin/hermesnag-mcp.sh

hermes mcp add hermesnag --command "$HOME/.hermes/bin/hermesnag-mcp.sh"
```

**Footgun:** `hermes mcp add` probes the server at add time; if the probe fails
(e.g. the HermesNag tunnel is down because the Mac app is closed), the entry is
saved with `enabled: false`. Check `~/.hermes/config.yaml` → `mcp_servers.hermesnag.enabled`
and re-test with `hermes mcp test hermesnag` once the tunnel is up.

Scope the tool to just this webhook route rather than every platform, by
hand-editing the subscription in `~/.hermes/webhook_subscriptions.json`
(deliberately not settable via CLI):

```json
"meetily-meeting": { ..., "toolsets": ["hermesnag"] }
```

## 4b. Deliver summaries to Telegram (optional, recommended)

By default the route has `deliver: "log"` — the agent's summary goes to the
gateway log only. To get it as a Telegram message, edit
`~/.hermes/webhook_subscriptions.json` and set on the route:

```json
"deliver": "telegram"
```

No `deliver_extra` needed: the gateway falls back to the Telegram **home
channel** (`TELEGRAM_HOME_CHANNEL` in `~/.hermes/.env`), i.e. the owner's DM.
Long messages are split automatically; markdown renders. Do NOT set
`deliver_only` (that would post the raw transcript prompt instead of the
summary). Routes hot-reload — no gateway restart. Remember the update footgun:
re-check `toolsets` after any `hermes webhook subscribe` re-run.

## 5. Smoke test

Signed sample POST (2xx proves auth + routing; `202 accepted` = agent run queued):

```sh
python3 - <<'EOF'
import hashlib, hmac, json, subprocess, time, urllib.request, uuid
secret = subprocess.run(["security", "find-generic-password", "-a", "meetily",
                         "-s", "meetily-webhook", "-w"],
                        capture_output=True, text=True).stdout.strip()
body = json.dumps({"event": "meeting.completed",
                   "meeting": {"id": "smoke-test", "title": "Smoke test",
                                "created_at": "2026-08-30T12:00:00+05:30",
                                "duration_seconds": 60},
                   "transcript": {"text": "test ping, no action items", "truncated": False},
                   }).encode()
ts = str(int(time.time()))
sig = hmac.new(secret.encode(), ts.encode() + b"." + body, hashlib.sha256).hexdigest()
req = urllib.request.Request("http://127.0.0.1:8644/webhooks/meetily-meeting",
    data=body, method="POST",
    headers={"Content-Type": "application/json", "X-Webhook-Signature-V2": sig,
             "X-Webhook-Timestamp": ts, "X-Request-ID": str(uuid.uuid4())})
with urllib.request.urlopen(req, timeout=10) as r:
    print(r.status, r.read().decode())
EOF
```

Then the real path:

```sh
meetily-mcp watch --once           # deliver any pending real meeting
tail -20 ~/.hermes/logs/gateway.log
```

Verify a task landed (HermesNag example):

```sh
curl -s -H "Authorization: Bearer $(security find-generic-password -a hermesnag -s hermesnag-token -w)" \
  "http://127.0.0.1:8787/tasks" | python3 -m json.tool | grep -A2 source_ref
```

## 6. Recovery / fallback

- Re-send a meeting after a bad agent run: `meetily-mcp replay <meeting_id>`.
- Gateway down: the watcher's outbox retries with backoff automatically.
- Manual one-shot ingestion without the gateway (zero-config fallback):
  `meetily-mcp watch --once --dry-run > /tmp/payload.json && hermes -z --query-file /tmp/payload.json`
