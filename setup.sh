#!/bin/bash
# meetily-connector guided installer for the Hermes stack.
#
#   /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/whoisdhana/meetily-connector/main/setup.sh)"
#
# Sets up: Meetily (brew cask) + meetily-connector (venv) + Gemini key
# (Keychain) + webhook route on your Hermes box (over SSH, with backup and
# confirmation) + SSH tunnel and watch daemons (launchd) + smoke test.
#
# Idempotent: every step detects existing state; safe to re-run after any
# failure. Flags:
#   --check-only        report state, write nothing, prompt for no secrets
#   --uninstall         remove launchd jobs; optionally venv/config/secrets
#   --local-port N      Mac-side tunnel port          (default 8645)
#   --remote-port N     box-side webhook adapter port (default 8644)
#   --ref REF           git ref of the connector to install (default main)

set -euo pipefail

REPO_URL="https://github.com/whoisdhana/meetily-connector"
LOCAL_PORT=8645
REMOTE_PORT=8644
REF="main"
CHECK_ONLY=0
UNINSTALL=0

CONNECTOR_HOME="$HOME/.meetily-connector"
VENV="$CONNECTOR_HOME/venv"
PY="$VENV/bin/python"
AGENTS="$HOME/Library/LaunchAgents"
WATCH_LABEL="ai.meetily.connector"
TUNNEL_LABEL="me.meetily.webhook-tunnel"
LOG="$CONNECTOR_HOME/setup.log"
MEETILY_DB="$HOME/Library/Application Support/com.meetily.ai/meeting_minutes.sqlite"
CURRENT_STEP="startup"

while [ $# -gt 0 ]; do
  case "$1" in
    --check-only) CHECK_ONLY=1 ;;
    --uninstall) UNINSTALL=1 ;;
    --local-port) LOCAL_PORT="$2"; shift ;;
    --remote-port) REMOTE_PORT="$2"; shift ;;
    --ref) REF="$2"; shift ;;
    -h|--help) grep '^#' "$0" | head -20; exit 0 ;;
    *) echo "unknown flag: $1" >&2; exit 2 ;;
  esac
  shift
done

# ---------- helpers ----------------------------------------------------------

log()  { printf '%s\n' "$*"; { printf '%s %s\n' "$(date +%FT%T)" "$*" >>"$LOG"; } 2>/dev/null || true; }
ok()   { log "  ✓ $*"; }
warn() { log "  ! $*"; }
die()  { log "  ✗ $*"; exit 1; }
step() { CURRENT_STEP="$*"; log ""; log "== $*"; }

on_error() {
  log ""
  log "FAILED during: $CURRENT_STEP"
  log "This installer is safe to re-run — completed steps are detected and skipped."
  log "Manual fallback for every step: $REPO_URL/blob/main/SETUP.md"
  log "Log: $LOG"
}
trap on_error ERR

# The single write gate: --check-only turns every mutation into a no-op.
do_write() {
  if [ "$CHECK_ONLY" = 1 ]; then
    log "  [check-only] would run: $*"
    return 0
  fi
  "$@"
}

confirm() {  # confirm "question" -> returns 0 on yes
  [ "$CHECK_ONLY" = 1 ] && return 1
  local reply
  read -r -p "$1 [y/N] " reply
  [ "$reply" = "y" ] || [ "$reply" = "Y" ]
}

keychain_has() { security find-generic-password -a "$2" -s "$1" >/dev/null 2>&1; }
keychain_get() { security find-generic-password -a "$2" -s "$1" -w; }

# ---------- uninstall --------------------------------------------------------

if [ "$UNINSTALL" = 1 ]; then
  step "Uninstall"
  for label in "$WATCH_LABEL" "$TUNNEL_LABEL"; do
    launchctl bootout "gui/$(id -u)/$label" 2>/dev/null && ok "stopped $label" || true
    if [ -f "$AGENTS/$label.plist" ]; then rm "$AGENTS/$label.plist"; ok "removed $label.plist"; fi
  done
  if [ -d "$VENV" ] && confirm "Remove the connector venv ($VENV)?"; then
    rm -rf "$VENV"; ok "removed venv"
  fi
  if [ -f "$CONNECTOR_HOME/config.toml" ] && confirm "Remove config + state ($CONNECTOR_HOME)?"; then
    rm -rf "$CONNECTOR_HOME"; ok "removed $CONNECTOR_HOME"
  fi
  for item in meetily-webhook gemini-api-key; do
    if keychain_has "$item" meetily && confirm "Remove Keychain item '$item'?"; then
      security delete-generic-password -a meetily -s "$item" >/dev/null; ok "removed $item"
    fi
  done
  log ""
  log "Note: the Meetily app, your recordings/notes, and the route on your"
  log "Hermes box were left in place. Remove the box route by editing"
  log "\$HOME/.hermes/webhook_subscriptions.json there (back it up first)."
  exit 0
fi

mkdir -p "$CONNECTOR_HOME" 2>/dev/null || true

log "meetily-connector installer $( [ "$CHECK_ONLY" = 1 ] && echo '(CHECK-ONLY: nothing will be written)')"

# ---------- 1. prerequisites -------------------------------------------------

step "1/10 Prerequisites"
FAILS=()
[ "$(uname -m)" = "arm64" ] || FAILS+=("Apple Silicon required (this Mac is $(uname -m)); Meetily's cask is arm64-only")
OSVER=$(sw_vers -productVersion)
OSMAJOR=${OSVER%%.*}; OSMINOR=$(echo "$OSVER" | cut -d. -f2)
if [ "$OSMAJOR" -lt 14 ] || { [ "$OSMAJOR" -eq 14 ] && [ "${OSMINOR:-0}" -lt 4 ]; }; then
  FAILS+=("macOS >= 14.4 required for system-audio capture (this Mac: $OSVER)")
fi
command -v brew >/dev/null || FAILS+=("Homebrew missing — install from https://brew.sh")
command -v git  >/dev/null || FAILS+=("git missing — run: xcode-select --install")
command -v ssh  >/dev/null || FAILS+=("ssh missing")
PYBIN=$(command -v python3 || true)
if [ -n "$PYBIN" ]; then
  PYOK=$("$PYBIN" -c 'import sys; print(1 if sys.version_info >= (3,11) else 0)')
  [ "$PYOK" = 1 ] || FAILS+=("python3 >= 3.11 required — run: brew install python@3.12")
else
  FAILS+=("python3 missing — run: brew install python@3.12")
fi
if [ ${#FAILS[@]} -gt 0 ]; then
  log "Cannot continue:"
  i=1; for f in "${FAILS[@]}"; do log "  $i. $f"; i=$((i+1)); done
  exit 1
fi
ok "Apple Silicon, macOS $OSVER, brew, git, ssh, python3 all present"

# ---------- 2. Meetily app ---------------------------------------------------

step "2/10 Meetily app"
if brew list --cask meetily >/dev/null 2>&1; then
  ok "Meetily already installed"
else
  do_write brew install --cask meetily
fi
if [ -f "$MEETILY_DB" ]; then
  ok "Meetily database present"
elif [ "$CHECK_ONLY" = 1 ]; then
  warn "Meetily database not created yet (app never launched)"
else
  log "  launching Meetily once to create its database..."
  open -a meetily || open -a Meetily || true
  waited=0
  while [ ! -f "$MEETILY_DB" ] && [ "$waited" -lt 60 ]; do sleep 5; waited=$((waited+5)); done
  if [ -f "$MEETILY_DB" ]; then
    ok "database created"
  else
    warn "database still missing — open Meetily manually and finish onboarding."
    warn "Continuing anyway (the watcher waits patiently for the database)."
  fi
fi

# ---------- 3. connector venv ------------------------------------------------

step "3/10 Connector"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]:-/}")" 2>/dev/null && pwd || true)"
if [ ! -x "$PY" ]; then
  do_write "$PYBIN" -m venv "$VENV"
fi
if [ -x "$PY" ]; then
  if [ -n "$SCRIPT_DIR" ] && [ -f "$SCRIPT_DIR/pyproject.toml" ]; then
    do_write "$PY" -m pip install -q --upgrade -e "$SCRIPT_DIR"
    ok "installed from local checkout (dev mode)"
  else
    do_write "$PY" -m pip install -q --upgrade "git+$REPO_URL@$REF"
    ok "installed from $REPO_URL@$REF"
  fi
  "$VENV/bin/meetily-mcp" --version >/dev/null && ok "meetily-mcp runs"
else
  warn "venv not present (check-only)"
fi

# ---------- 4. Gemini key ----------------------------------------------------

step "4/10 Gemini transcription key"
TRANSCRIBER="gemini"
if keychain_has gemini-api-key meetily; then
  ok "Gemini key already in Keychain"
elif [ "$CHECK_ONLY" = 1 ]; then
  warn "no Gemini key in Keychain"
else
  log "  A Google Gemini API key gives high-quality multilingual transcription"
  log "  (free tier available: https://aistudio.google.com/apikey)."
  log "  Paste your key (input hidden), or press Enter to skip:"
  read -rs GEMINI_KEY; echo
  if [ -z "$GEMINI_KEY" ]; then
    TRANSCRIBER="meetily"
    warn "skipped — using Meetily's local transcript (weaker for non-English)"
  else
    HTTP=$(curl -s -o /dev/null -w '%{http_code}' --max-time 15 \
      -H "x-goog-api-key: $GEMINI_KEY" \
      "https://generativelanguage.googleapis.com/v1beta/models?pageSize=1" || echo 000)
    if [ "$HTTP" = "200" ]; then
      security add-generic-password -U -a meetily -s gemini-api-key -w "$GEMINI_KEY"
      ok "key validated and stored in Keychain"
    else
      die "key validation failed (HTTP $HTTP) — get a key at https://aistudio.google.com/apikey and re-run"
    fi
    unset GEMINI_KEY
  fi
fi

# ---------- 5. SSH host ------------------------------------------------------

step "5/10 Hermes box SSH access"
SSH_HOST=""
if [ -f "$AGENTS/$TUNNEL_LABEL.plist" ]; then
  SSH_HOST=$(/usr/libexec/PlistBuddy -c "Print :ProgramArguments" "$AGENTS/$TUNNEL_LABEL.plist" 2>/dev/null | tail -2 | head -1 | tr -d ' ' || true)
  [ -n "$SSH_HOST" ] && ok "existing tunnel uses host '$SSH_HOST'"
fi
if [ -z "$SSH_HOST" ]; then
  if [ "$CHECK_ONLY" = 1 ]; then
    warn "no tunnel configured yet"
  else
    if [ -f "$HOME/.ssh/config" ]; then
      log "  Hosts in your ~/.ssh/config:"
      awk '/^Host /{for(i=2;i<=NF;i++) if ($i !~ /[*?]/) print "    - " $i}' "$HOME/.ssh/config" | head -10
    fi
    read -r -p "  SSH host alias of your Hermes box: " SSH_HOST
    [ -n "$SSH_HOST" ] || die "an SSH host is required"
  fi
fi
if [ -n "$SSH_HOST" ]; then
  if ssh -o BatchMode=yes -o ConnectTimeout=10 "$SSH_HOST" true 2>/dev/null; then
    ok "key-based SSH to '$SSH_HOST' works"
  else
    die "cannot ssh to '$SSH_HOST' without a password — set up an SSH key first (ssh-copy-id $SSH_HOST)"
  fi
fi

# ---------- 6. box checks ----------------------------------------------------

step "6/10 Hermes box checks (read-only)"
if [ -n "$SSH_HOST" ]; then
  while :; do
    ADAPTER=fail; MCP=warn; TG=warn; HV="?"
    ssh -o BatchMode=yes "$SSH_HOST" "ss -tln 2>/dev/null | grep -q ':$REMOTE_PORT '" && ADAPTER=pass
    ssh -o BatchMode=yes "$SSH_HOST" "grep -rqs hermesnag ~/.hermes/config.yaml" && MCP=pass
    ssh -o BatchMode=yes "$SSH_HOST" "grep -qs '^TELEGRAM_HOME_CHANNEL=' ~/.hermes/.env" && TG=pass
    HV=$(ssh -o BatchMode=yes "$SSH_HOST" "grep -o '\"code_version\": \"[^\"]*\"' ~/.hermes/gateway_state.json 2>/dev/null | head -1 | cut -d'\"' -f4" || true)
    log "  webhook adapter on :$REMOTE_PORT : $ADAPTER"
    log "  hermesnag MCP configured     : $MCP  $( [ $MCP = warn ] && echo '(tasks cannot be created until wired — see SETUP.md)')"
    log "  Telegram home channel set    : $TG  $( [ $TG = warn ] && echo '(summaries will have nowhere to go)')"
    log "  Hermes version               : ${HV:-unknown}"
    [ "$ADAPTER" = pass ] && { ok "adapter is listening — no box config changes needed (routes hot-reload)"; break; }
    [ "$CHECK_ONLY" = 1 ] && { warn "adapter not listening"; break; }
    log ""
    log "  The webhook adapter is not listening. On the box, add BOTH blocks to"
    log "  ~/.hermes/config.yaml (Hermes 0.20.x reads them from different places):"
    log ""
    log "    # top-level (read by the hermes CLI)"
    log "    platforms:"
    log "      webhook:"
    log "        enabled: true"
    log "        extra: { host: \"127.0.0.1\", port: $REMOTE_PORT }"
    log ""
    log "    # under gateway: (read by the running gateway)"
    log "    gateway:"
    log "      platforms:"
    log "        webhook:"
    log "          enabled: true"
    log "          extra: { host: \"127.0.0.1\", port: $REMOTE_PORT }"
    log ""
    log "  then restart the gateway on the box, and press Enter to re-check."
    read -r -p "  Enter to re-check, q to abort: " REPLY
    [ "$REPLY" = "q" ] && exit 1
  done
fi

# ---------- 7. secret + route ------------------------------------------------

step "7/10 HMAC secret + box route"
if keychain_has meetily-webhook meetily; then
  ok "webhook secret already in Keychain (reusing — keeps Mac and box in sync)"
else
  if [ "$CHECK_ONLY" = 1 ]; then
    warn "no webhook secret yet"
  else
    SECRET=$("$PYBIN" -c 'import secrets; print(secrets.token_hex(32))')
    security add-generic-password -U -a meetily -s meetily-webhook -w "$SECRET"
    unset SECRET
    ok "generated and stored webhook secret"
  fi
fi
if [ -n "$SSH_HOST" ] && [ "$CHECK_ONLY" != 1 ]; then
  SUBS_FILE=".hermes/webhook_subscriptions.json"
  EXISTING=$(ssh -o BatchMode=yes "$SSH_HOST" "cat $SUBS_FILE 2>/dev/null" || true)
  MERGED=$( { keychain_get meetily-webhook meetily; printf '%s' "${EXISTING:-{\}}"; } | "$PY" -m meetily_connector.route merge ) || die "route merge failed"
  log ""
  if confirm "  Write this route to $SSH_HOST:~/$SUBS_FILE (existing file backed up)?"; then
    ssh -o BatchMode=yes "$SSH_HOST" "[ -f $SUBS_FILE ] && cp $SUBS_FILE $SUBS_FILE.bak.\$(date +%s) || true"
    printf '%s' "$MERGED" | ssh -o BatchMode=yes "$SSH_HOST" "cat > $SUBS_FILE.tmp && chmod 600 $SUBS_FILE.tmp && mv $SUBS_FILE.tmp $SUBS_FILE"
    ok "route written (hot-reloads on next request — no gateway restart needed)"
  else
    warn "route not written — the pipeline will not reach your box until it is"
  fi
  unset MERGED
elif [ -n "$SSH_HOST" ]; then
  log "  [check-only] would merge the meetily-meeting route into the box's webhook_subscriptions.json"
fi

# ---------- 8. config + launchd ---------------------------------------------

step "8/10 Connector config + daemons"
if [ -x "$PY" ]; then
  WRITE_FLAG="--write"; [ "$CHECK_ONLY" = 1 ] && WRITE_FLAG=""
  # shellcheck disable=SC2086  # WRITE_FLAG is intentionally empty in check-only
  "$PY" -m meetily_connector.install render-config \
      --local-port "$LOCAL_PORT" --transcriber "$TRANSCRIBER" $WRITE_FLAG || true
  if [ -n "$SSH_HOST" ]; then
    # shellcheck disable=SC2086
    "$PY" -m meetily_connector.install render-plists \
        --ssh-host "$SSH_HOST" --python "$PY" \
        --local-port "$LOCAL_PORT" --remote-port "$REMOTE_PORT" $WRITE_FLAG || true
  fi
  if [ "$CHECK_ONLY" != 1 ]; then
    for label in "$TUNNEL_LABEL" "$WATCH_LABEL"; do
      launchctl bootout "gui/$(id -u)/$label" 2>/dev/null || true
      launchctl bootstrap "gui/$(id -u)" "$AGENTS/$label.plist"
      launchctl kickstart -k "gui/$(id -u)/$label" 2>/dev/null || true
      ok "$label running"
    done
  fi
else
  warn "connector not installed yet (check-only)"
fi

# ---------- 9. smoke test ----------------------------------------------------

step "9/10 Smoke test"
if [ "$CHECK_ONLY" = 1 ]; then
  if curl -fsS --max-time 5 "http://127.0.0.1:$LOCAL_PORT/health" >/dev/null 2>&1; then
    ok "tunnel healthy"
  else
    warn "tunnel not reachable"
  fi
else
  waited=0
  until curl -fsS --max-time 3 "http://127.0.0.1:$LOCAL_PORT/health" >/dev/null 2>&1; do
    [ "$waited" -ge 15 ] && break
    sleep 3; waited=$((waited+3))
  done
  if curl -fsS --max-time 5 "http://127.0.0.1:$LOCAL_PORT/health" >/dev/null 2>&1; then
    ok "tunnel healthy"
    if "$VENV/bin/meetily-mcp" smoke; then
      ok "signed test payload accepted by your Hermes box"
    else
      warn "smoke test failed — see message above; fix and re-run this script"
    fi
  else
    warn "tunnel to $SSH_HOST:$REMOTE_PORT not up yet. Check:"
    warn "  launchctl print gui/\$(id -u)/$TUNNEL_LABEL"
    warn "  tail -5 $CONNECTOR_HOME/logs/tunnel.error.log"
  fi
fi

# ---------- 10. epilogue -----------------------------------------------------

step "10/10 One manual step + first meeting"
cat <<'EOT'

  REQUIRED (no script can do this for you):
    System Settings → Privacy & Security → Screen & System Audio Recording
      → under "System Audio Recording Only", enable  meetily

    Do not trust Meetily's own permission screen — it can claim success while
    recording silence. The REAL test: open Meetily's device picker and check
    that BOTH level meters move (speak for the mic; play any video for the
    system meter).

  FIRST MEETING TEST:
    1. Play any video with speech, record ~90 seconds in Meetily, stop.
    2. Within ~2 minutes you should get: a Telegram message from your Hermes
       bot, a notes file in ~/Documents/MeetingNotes/, and any action items
       as tasks.

EOT
log "Installed:"
log "  config     $CONNECTOR_HOME/config.toml"
log "  daemons    $WATCH_LABEL, $TUNNEL_LABEL (launchd, auto-start)"
log "  logs       $CONNECTOR_HOME/logs/"
log "  status     $VENV/bin/meetily-mcp status"
[ -x "$VENV/bin/meetily-mcp" ] && "$VENV/bin/meetily-mcp" status || true
log ""
log "Done. Full docs: $REPO_URL"
