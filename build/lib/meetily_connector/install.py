"""init / status subcommands: config bootstrap, launchd rendering, health view.

Also exposes non-interactive renderers for setup.sh:
    python -m meetily_connector.install render-config --ssh-host X [...]
    python -m meetily_connector.install render-plists --ssh-host X --python P [...]
Renderers print a unified diff against any existing file and only write when
--write is passed, so the installer can show-then-confirm.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from .config import (
    DB_CANDIDATES,
    DEFAULT_CONFIG_DIR,
    DEFAULT_CONFIG_PATH,
    Config,
    ConfigError,
)

_PLIST_LABEL = "ai.meetily.connector"


def _example_config_text() -> str:
    example = Path(__file__).resolve().parents[2] / "deploy" / "config.example.toml"
    if example.exists():
        return example.read_text(encoding="utf-8")
    # Installed without the deploy/ tree (e.g. pip): minimal skeleton.
    return (
        "[meetily]\n"
        'db_path = ""\n\n'
        "[watch]\n"
        'webhook_url = ""\n'
        'secret_source = "env"\n'
        'secret_env = "MEETILY_WEBHOOK_SECRET"\n'
    )


def run_init(cfg: Config, launchd: bool = False) -> int:
    DEFAULT_CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
    (DEFAULT_CONFIG_DIR / "logs").mkdir(mode=0o700, exist_ok=True)

    if DEFAULT_CONFIG_PATH.exists():
        print(f"config already exists: {DEFAULT_CONFIG_PATH} (left untouched)")
    else:
        DEFAULT_CONFIG_PATH.write_text(_example_config_text(), encoding="utf-8")
        os.chmod(DEFAULT_CONFIG_PATH, 0o600)
        print(f"wrote {DEFAULT_CONFIG_PATH}")
        print("edit it: set [watch] webhook_url and a secret source, then test with")
        print("  meetily-mcp watch --once --dry-run")

    if launchd:
        _render_launchd()
    return 0


def _render_launchd() -> None:
    home = str(Path.home())
    src_dir = str(Path(__file__).resolve().parents[1])
    rendered = _render_template(f"{_PLIST_LABEL}.plist", {
        "__PYTHON__": sys.executable,
        "__HOME__": home,
        "__PYTHONPATH__": src_dir,
    })
    out = DEFAULT_CONFIG_DIR / f"{_PLIST_LABEL}.plist"
    out.write_text(rendered, encoding="utf-8")
    print(f"\nrendered {out}")
    print("install it (not done automatically):")
    print(f"  cp {out} ~/Library/LaunchAgents/")
    print(f"  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{_PLIST_LABEL}.plist")
    print("check:")
    print(f"  launchctl print gui/$(id -u)/{_PLIST_LABEL} | head -20")
    print(f"  tail -f {DEFAULT_CONFIG_DIR}/logs/watch.error.log")


def _config_toml(local_port: int, transcriber: str, language_hint: str) -> str:
    return f'''# meetily-connector configuration (written by setup)
# Secrets live in the macOS Keychain, never in this file.

[meetily]
db_path = ""

[watch]
trigger = "transcript"
poll_interval_seconds = 30
min_duration_seconds = 60
webhook_url = "http://127.0.0.1:{local_port}/webhooks/meetily-meeting"
allow_insecure_transport = false
language_hint = "{language_hint}"
secret_source = "keychain"
secret_keychain_service = "meetily-webhook"
secret_keychain_account = "meetily"

[transcribe]
transcriber = "{transcriber}"
gemini_model = "gemini-3.5-transcribe"
gemini_language_codes = []
gemini_diarization = true
gemini_key_source = "keychain"
gemini_key_keychain_service = "gemini-api-key"
gemini_key_keychain_account = "meetily"

[notes]
enabled = true
dir = "~/Documents/MeetingNotes"
summary_model = "gemini-3.5-flash-lite"
language = "en"

[retry]
base_seconds = 30
max_seconds = 3600
max_attempts = 0
'''


def _diff_and_maybe_write(target: Path, content: str, write: bool) -> int:
    """Print status/diff; write only when asked. Returns 0=no change needed,
    3=differs (diff printed; written only if write=True)."""
    import difflib

    existing = target.read_text(encoding="utf-8") if target.exists() else ""
    if existing == content:
        print(f"unchanged: {target}")
        return 0
    diff = difflib.unified_diff(
        existing.splitlines(keepends=True), content.splitlines(keepends=True),
        fromfile=f"existing {target.name}", tofile=f"new {target.name}")
    sys.stdout.writelines(diff)
    if write:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(content, encoding="utf-8")
        os.chmod(target, 0o600)
        print(f"\nwrote: {target}")
    else:
        print(f"\nwould write: {target} (pass --write to apply)")
    return 3


def _render_template(name: str, replacements: dict[str, str]) -> str:
    # Packaged location first (venv installs); repo deploy/ as dev fallback.
    template = Path(__file__).resolve().parent / "templates" / name
    if not template.exists():
        template = Path(__file__).resolve().parents[2] / "deploy" / name
    text = template.read_text(encoding="utf-8")
    for key, value in replacements.items():
        text = text.replace(key, value)
    return text


def cmd_render_config(args) -> int:
    content = _config_toml(args.local_port, args.transcriber, args.language_hint)
    return _diff_and_maybe_write(DEFAULT_CONFIG_PATH, content, args.write)


def cmd_render_plists(args) -> int:
    home = str(Path.home())
    agents = Path(home) / "Library" / "LaunchAgents"
    (DEFAULT_CONFIG_DIR / "logs").mkdir(parents=True, exist_ok=True)

    watch = _render_template(f"{_PLIST_LABEL}.plist", {
        "__PYTHON__": args.python,
        "__HOME__": home,
        "__PYTHONPATH__": args.pythonpath or "",
    })
    tunnel = _render_template("me.meetily.webhook-tunnel.plist", {
        "__LOCAL_PORT__": str(args.local_port),
        "__REMOTE_PORT__": str(args.remote_port),
        "__SSH_HOST__": args.ssh_host,
        "__HOME__": home,
    })
    rc1 = _diff_and_maybe_write(agents / f"{_PLIST_LABEL}.plist", watch, args.write)
    rc2 = _diff_and_maybe_write(agents / "me.meetily.webhook-tunnel.plist",
                                tunnel, args.write)
    return max(rc1, rc2)


def main(argv: list[str] | None = None) -> int:
    import argparse

    parser = argparse.ArgumentParser(prog="meetily_connector.install")
    sub = parser.add_subparsers(dest="cmd", required=True)

    rc = sub.add_parser("render-config")
    rc.add_argument("--local-port", type=int, default=8645)
    rc.add_argument("--transcriber", default="gemini",
                    choices=["gemini", "meetily"])
    rc.add_argument("--language-hint", default="")
    rc.add_argument("--write", action="store_true")

    rp = sub.add_parser("render-plists")
    rp.add_argument("--ssh-host", required=True)
    rp.add_argument("--python", required=True,
                    help="absolute python for the watch daemon")
    rp.add_argument("--pythonpath", default="")
    rp.add_argument("--local-port", type=int, default=8645)
    rp.add_argument("--remote-port", type=int, default=8644)
    rp.add_argument("--write", action="store_true")

    args = parser.parse_args(argv)
    if args.cmd == "render-config":
        return cmd_render_config(args)
    if args.cmd == "render-plists":
        return cmd_render_plists(args)
    return 2


def run_status(cfg: Config) -> int:
    print(f"meetily-connector status")
    print(f"  config: {DEFAULT_CONFIG_PATH} "
          f"({'present' if DEFAULT_CONFIG_PATH.exists() else 'missing'})")

    try:
        db_path = cfg.resolve_db_path()
        print(f"  meetily db: {db_path}")
        from .db import MeetilyDB

        with MeetilyDB(db_path) as db:
            counts = db.counts()
        for key, value in counts.items():
            print(f"    {key}: {value}")
    except ConfigError as exc:
        print(f"  meetily db: NOT FOUND")
        for candidate in DB_CANDIDATES:
            print(f"    probed {Path(candidate).expanduser()}")
    except Exception as exc:  # locked, schema drift, …
        print(f"  meetily db: error ({type(exc).__name__})")

    state_db = cfg.state_dir / "state.db"
    if state_db.exists():
        from .outbox import StateStore

        store = StateStore(cfg)
        try:
            sent = store.conn.execute(
                "SELECT disposition, COUNT(*) FROM sent_log GROUP BY disposition"
            ).fetchall()
            print(f"  outbox depth: {store.depth()}")
            for disposition, count in sent:
                print(f"  sent_log {disposition}: {count}")
        finally:
            store.close()
    else:
        print("  state db: not created yet (watch mode never ran)")

    if cfg.webhook_url:
        print(f"  webhook_url: {cfg.webhook_url}")
    else:
        print("  webhook_url: (unset — watch mode disabled)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
