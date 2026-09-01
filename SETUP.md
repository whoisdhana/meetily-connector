# Setup guide — Meetily → Hermes meeting pipeline

This sets up the full pipeline on your Mac:

```
You record a meeting in Meetily (works with Google Meet, Zoom, anything —
it captures the Mac's audio directly)
  → transcript (optionally via Google Gemini, great for non-English/code-mixed speech)
  → notes file on your Mac (summary + action items + transcript)
  → your Hermes agent on your server extracts action items
  → tasks appear in HermesNag, summary arrives on Telegram
```

## Prerequisites

| Requirement | Why |
|---|---|
| Apple Silicon Mac (M1+), macOS **14.4+** | Meetily + system-audio capture |
| Homebrew, git, python3 ≥ 3.11 | installer basics |
| A server running a **Hermes agent** gateway (v0.20.x) with the **hermesnag** MCP wired | the brain of the pipeline |
| Key-based SSH access to that server (`ssh <host>` with no password) | tunnel + route setup |
| *(optional but recommended)* a free [Gemini API key](https://aistudio.google.com/apikey) | high-quality multilingual transcription |

## Install

```sh
/bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/whoisdhana/meetily-connector/main/setup.sh)"
```

The wizard is **idempotent** — if anything fails, fix it and re-run; completed
steps are detected and skipped. To see what it would do without changing
anything: append `--check-only`.

It will ask you for exactly three things:
1. **Your Gemini API key** (input hidden; stored in the macOS Keychain, validated live; Enter to skip).
2. **The SSH host alias of your Hermes box** (it lists candidates from your `~/.ssh/config`).
3. **Confirmation before writing the route to your box** (existing file is backed up first; your other webhook routes are never touched).

Everything else is automatic: Meetily install, the connector, the SSH tunnel
and watch daemons (auto-start at login), and a signed end-to-end smoke test.

## The one manual step

macOS will not let a script grant audio permissions. After install:

1. Open **System Settings → Privacy & Security → Screen & System Audio Recording**.
2. Under **"System Audio Recording Only"**, enable **meetily**.
   (Also confirm meetily is enabled under **Microphone**.)
3. **Verify with the level meters**: open Meetily's device picker — speak (mic
   meter moves) and play any video (system meter moves). *Do not trust
   Meetily's own permission screens — they can report success while recording
   silence. Moving meters are the only real proof.*

## First test

1. Play any video with clear speech; **record ~90 seconds** in Meetily; stop.
2. Within ~2 minutes: a Telegram message from your Hermes bot with the summary,
   a notes file in `~/Documents/MeetingNotes/`, and tasks for any action items.

Then in real meetings: join the call (headphones recommended — avoids echo),
press record in Meetily, stop when done. Recordings under ~1 minute are
skipped on purpose (accidental-recording guard). Ignore the transcript shown
inside Meetily itself — the pipeline uses its own, better transcription.

## Troubleshooting

**Tunnel not healthy / smoke test can't connect**
```sh
launchctl print gui/$(id -u)/me.meetily.webhook-tunnel | head -5
tail -5 ~/.meetily-connector/logs/tunnel.error.log
ssh <host> true   # must work without a password
```

**Smoke test returns 401** — the HMAC secret on your Mac and the route on the
box disagree. Re-run `setup.sh`; step 7 reuses the Keychain secret and rewrites
the route to match.

**"Webhook adapter is not listening" during install** — on your box, Hermes
0.20.x reads the webhook platform config from **two** places; you need both
blocks in `~/.hermes/config.yaml` (the installer prints them), then restart
your gateway. Once the adapter is up, routes hot-reload — future changes need
no restart.

**Telegram message never arrives** — check `TELEGRAM_HOME_CHANNEL` is set in
the box's `~/.hermes/.env` (that's the chat summaries go to), and that your
gateway's Telegram platform shows connected.

**No tasks created** — the box needs the `hermesnag` MCP server registered in
its Hermes config; without it the agent can summarize but can't write tasks.

**Meetily shows garbage transcripts** — expected; its local model is weak.
The pipeline's transcript (in your notes file) is what counts.

**Watcher state** — `~/.meetily-connector/venv/bin/meetily-mcp status`, logs in
`~/.meetily-connector/logs/watch.error.log`. Re-send a meeting:
`meetily-mcp replay <meeting_id>`.

## Manual steps appendix (wizard fallback)

If the wizard fails mid-way, every step below is the exact manual equivalent —
continue from where it stopped.

```sh
# 2. Meetily
brew install --cask meetily && open -a meetily   # finish onboarding once

# 3. Connector
python3 -m venv ~/.meetily-connector/venv
~/.meetily-connector/venv/bin/pip install "git+https://github.com/whoisdhana/meetily-connector@main"

# 4. Gemini key (paste at the invisible prompt)
read -rs KEY && security add-generic-password -U -a meetily -s gemini-api-key -w "$KEY" && unset KEY

# 6. Box check: webhook adapter listening?
ssh <host> "ss -tln | grep 8644"

# 7. Secret + route (merges safely; other routes untouched)
python3 -c 'import secrets;print(secrets.token_hex(32))' | \
  xargs -I{} security add-generic-password -U -a meetily -s meetily-webhook -w {}
{ security find-generic-password -a meetily -s meetily-webhook -w; \
  ssh <host> "cat ~/.hermes/webhook_subscriptions.json 2>/dev/null"; } | \
  ~/.meetily-connector/venv/bin/python -m meetily_connector.route merge > /tmp/subs.json
ssh <host> "cp ~/.hermes/webhook_subscriptions.json{,.bak} 2>/dev/null; cat > ~/.hermes/webhook_subscriptions.json && chmod 600 ~/.hermes/webhook_subscriptions.json" < /tmp/subs.json
rm /tmp/subs.json

# 8. Config + daemons
~/.meetily-connector/venv/bin/python -m meetily_connector.install render-config --write
~/.meetily-connector/venv/bin/python -m meetily_connector.install render-plists \
  --ssh-host <host> --python ~/.meetily-connector/venv/bin/python --write
for l in me.meetily.webhook-tunnel ai.meetily.connector; do
  launchctl bootout gui/$(id -u)/$l 2>/dev/null; \
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/$l.plist; done

# 9. Smoke
curl -fsS http://127.0.0.1:8645/health
~/.meetily-connector/venv/bin/meetily-mcp smoke
```
