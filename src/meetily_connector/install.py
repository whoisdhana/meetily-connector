"""init / status subcommands: config bootstrap, launchd rendering, health view."""

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
    template = Path(__file__).resolve().parents[2] / "deploy" / f"{_PLIST_LABEL}.plist"
    if not template.exists():
        print(f"plist template not found at {template}", file=sys.stderr)
        return
    home = str(Path.home())
    src_dir = str(Path(__file__).resolve().parents[1])
    rendered = (
        template.read_text(encoding="utf-8")
        .replace("__PYTHON__", sys.executable)
        .replace("__HOME__", home)
        .replace("__PYTHONPATH__", src_dir)
    )
    out = DEFAULT_CONFIG_DIR / f"{_PLIST_LABEL}.plist"
    out.write_text(rendered, encoding="utf-8")
    print(f"\nrendered {out}")
    print("install it (not done automatically):")
    print(f"  cp {out} ~/Library/LaunchAgents/")
    print(f"  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/{_PLIST_LABEL}.plist")
    print("check:")
    print(f"  launchctl print gui/$(id -u)/{_PLIST_LABEL} | head -20")
    print(f"  tail -f {DEFAULT_CONFIG_DIR}/logs/watch.error.log")


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
