"""CLI entry point: meetily-mcp <command>.

Commands:
  serve            stdio MCP server (JSON-RPC 2.0 over stdin/stdout)
  watch            webhook watcher daemon (--once for a single tick,
                   --dry-run to print payloads without sending)
  init             create ~/.meetily-connector/config.toml (--launchd renders
                   the launchd plist and prints the bootstrap command)
  status           show db discovery, meeting counts, outbox depth
  replay ID        forget a meeting in sent_log and enqueue it again
"""

from __future__ import annotations

import argparse
import sys

from . import __version__
from .config import ConfigError, load_config
from .logging_setup import setup_logging


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meetily-mcp",
        description="Bridge Meetily meeting data to any agent (MCP server + webhook watcher).",
    )
    parser.add_argument("--version", action="version", version=f"%(prog)s {__version__}")
    parser.add_argument("--config", help="path to config.toml (default ~/.meetily-connector/config.toml)")
    parser.add_argument("-v", "--verbose", action="store_true", help="debug logging to stderr")

    sub = parser.add_subparsers(dest="command", required=True)

    sub.add_parser("serve", help="run the stdio MCP server")

    watch = sub.add_parser("watch", help="run the webhook watcher")
    watch.add_argument("--once", action="store_true", help="single tick, then exit")
    watch.add_argument("--dry-run", action="store_true",
                       help="print payloads to stdout instead of POSTing; no state writes")

    init = sub.add_parser("init", help="create the default config")
    init.add_argument("--launchd", action="store_true",
                      help="also render the launchd plist and print install instructions")

    sub.add_parser("status", help="show connector status")

    replay = sub.add_parser("replay", help="re-enqueue a meeting")
    replay.add_argument("meeting_id")

    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    setup_logging(verbose=args.verbose)

    try:
        from pathlib import Path

        config_path = Path(args.config).expanduser() if args.config else None
        cfg = load_config(config_path)

        if args.command == "serve":
            from .mcp_server import serve

            return serve(cfg)
        if args.command == "watch":
            from .watcher import run_watch

            return run_watch(cfg, once=args.once, dry_run=args.dry_run)
        if args.command == "init":
            from .install import run_init

            return run_init(cfg, launchd=args.launchd)
        if args.command == "status":
            from .install import run_status

            return run_status(cfg)
        if args.command == "replay":
            from .outbox import run_replay

            return run_replay(cfg, args.meeting_id)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
