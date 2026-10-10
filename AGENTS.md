# AGENTS.md: meetily-connector

Bridges Meetily (local meeting recorder) transcripts to any agent: stdio MCP server + signed webhook watcher. Public repo, MIT.
Python 3.11+, stdlib only at runtime (no dependencies), pytest for tests. Runs on the user's Mac (launchd); the Hermes box receives webhooks.

## Layout

- `src/meetily_connector/`: the package (`cli.py` entry `meetily-mcp`, `db.py`, `watcher.py`, `webhook.py`,
  `mcp_server.py`, `gemini_transcribe.py`, `notes.py`, `outbox.py`, `install.py`, `secrets.py`, `templates/*.plist`).
- `tests/`: pytest suite; `tests/fixtures/` builds a synthetic Meetily DB (`schema.sql`, `build_fixture.py`).
- `deploy/`: example config, launchd plists, Hermes webhook subscription guide.
- `setup.sh`: guided installer for the Hermes stack (Meetily, venv, Keychain, route over SSH, launchd).
- `docs/`: Meetily schema notes, Google Meet recording guide.

## Commands (run from repo root)

| Step | Command | Notes |
|---|---|---|
| Install | `python3.12 -m venv .venv && .venv/bin/pip install -e ".[dev]"` | `.venv/` is gitignored. |
| Test | `.venv/bin/python -m pytest -q` | 61 tests, all pass, offline (synthetic fixture DB). |
| CLI smoke | `.venv/bin/meetily-mcp --help` | Subcommands: serve, watch, init, status, smoke, replay. |
| Installer syntax | `bash -n setup.sh` | Do NOT run `setup.sh` itself on this box (see Gotchas). |
| Lint / typecheck | none | No linter configured; tests are the gate. |
| Whitespace | `git diff --check` | Must be clean. |

There is no CI workflow. Run the suite yourself and paste the result.

## Definition of done (before opening a PR)

1. `.venv/bin/python -m pytest -q` passes, with new tests for new behaviour.
2. Runtime stays stdlib-only (`dependencies = []` in `pyproject.toml`); dev deps only under `[dev]`.
3. `git diff --check` is clean; the diff contains only the requested change.
4. If `src/meetily_connector/templates/*.plist` changes, keep `deploy/*.plist` in sync.
5. PR from a feature branch.

## Accounts and deploy target

- GitHub: `whoisdhana/meetily-connector` (PUBLIC), default branch `main`. Use `gx whoisdhana ...`
  (pinned by `.gx-account`). Commit author `whoisdhana <whoisdhana@gmail.com>` (gx sets it).
- Not hosted on Vercel; no `.vercel-account`. No package registry release.
- "Deploy" = users run `setup.sh` from `raw.githubusercontent.com/.../main/setup.sh`, which
  installs the connector at `--ref main` by default. So a merge to `main` ships to every new
  or re-run install immediately.
- Dhana's own install lives on his Mac (`~/.meetily-connector/`, launchd jobs
  `ai.meetily.connector` and `me.meetily.webhook-tunnel`), tunnelled to the Hermes webhook
  adapter on the box (route `meetily-meeting`, loopback port 8644).

## Needs Dhana's explicit yes

- Merging or pushing to `main` (that is a release, see above).
- Running `setup.sh` (non `--check-only`) anywhere, or touching his Mac install or launchd jobs.
- Changing the Hermes webhook route, adapter config in `~/.hermes/config.yaml`, or restarting
  a gateway.
- Tags, releases, version bumps; anything that sends Telegram messages or creates real tasks.

Reading, tests, feature branches and draft PRs need no approval.

## Gotchas

- Meetily's DB must stay read-only (`mode=ro` + `PRAGMA query_only`). Never add writes.
- Transcript text never goes into logs: only ids, sizes and statuses. Keep it that way.
- Secrets come from env, macOS Keychain or 0600 files only; never log or commit them.
  Never read `~/.meetily-connector/` contents, Keychain items, or `~/.hermes/.env`.
- Plain `http://` to non-loopback hosts is refused unless `allow_insecure_transport = true`.
- `build/lib/meetily_connector/` is a stale tracked build copy. Edit `src/` only; do not
  "sync" build/ unless asked (removing it is a separate change).
- PUBLIC repo: no personal hostnames, IPs, chat ids, meeting content or tokens in code,
  tests, fixtures, docs or commits. Fixtures must stay synthetic.
- Hermes 0.20.x reads the webhook platform config from two places; both blocks are needed
  (see SETUP.md troubleshooting).
- Commit history uses `dhana@aggrandizedigital.com`; new commits use whoisdhana@gmail.com.

## Longer docs

- `README.md`: modes, watcher payload (`meeting.completed` v1), security posture.
- `SETUP.md`: guided install, the one manual step, troubleshooting, manual fallback.
- `deploy/hermes-webhook-subscription.md`: Hermes route setup.
- `docs/schema-notes.md`: Meetily SQLite schema. `docs/google-meet-recording.md`.
