"""stdio MCP server: JSON-RPC 2.0, one JSON object per line.

stdout carries protocol frames only; logs go to stderr. Read-only over
Meetily's DB. Errors surface as tool results with isError so a bad call
never kills the loop.
"""

from __future__ import annotations

import json
import sys

from . import __version__
from .config import Config, ConfigError
from .db import DatabaseBusy, MeetilyDB
from .logging_setup import log_event

SERVER_NAME = "meetily"
PROTOCOL_VERSION = "2024-11-05"

TOOLS = [
    {
        "name": "list_meetings",
        "description": "List recent Meetily meetings (newest first) with id, "
                       "title, creation time, and summary status.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "limit": {"type": "integer", "description": "max results (default 20, cap 100)"},
                "since": {"type": "string", "description": "ISO date lower bound, e.g. 2026-08-01"},
                "until": {"type": "string", "description": "ISO date upper bound"},
            },
        },
    },
    {
        "name": "get_meeting",
        "description": "Get one meeting's metadata and its Meetily-generated "
                       "summary status/result.",
        "inputSchema": {
            "type": "object",
            "properties": {"meeting_id": {"type": "string"}},
            "required": ["meeting_id"],
        },
    },
    {
        "name": "get_transcript",
        "description": "Get a meeting's full transcript (segments concatenated "
                       "in audio order). Paged: pass offset to continue after "
                       "a truncation marker.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "meeting_id": {"type": "string"},
                "max_chars": {"type": "integer", "description": "default 50000"},
                "offset": {"type": "integer", "description": "character offset to start from"},
            },
            "required": ["meeting_id"],
        },
    },
    {
        "name": "search_meetings",
        "description": "Substring search across meeting titles and transcripts; "
                       "returns matches with a context snippet.",
        "inputSchema": {
            "type": "object",
            "properties": {
                "query": {"type": "string"},
                "limit": {"type": "integer", "description": "default 10, cap 50"},
            },
            "required": ["query"],
        },
    },
    {
        "name": "get_summary",
        "description": "Get Meetily's own generated summary JSON for a meeting, "
                       "or its status/error if not completed.",
        "inputSchema": {
            "type": "object",
            "properties": {"meeting_id": {"type": "string"}},
            "required": ["meeting_id"],
        },
    },
    {
        "name": "connector_status",
        "description": "Connector health: database discovery, meeting counts, "
                       "schema drift warnings, watch-mode outbox depth.",
        "inputSchema": {"type": "object", "properties": {}},
    },
]


def serve(cfg: Config) -> int:
    log_event("mcp.serving", version=__version__)
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            request = json.loads(line)
        except ValueError:
            _send_error(None, -32700, "parse error")
            continue
        response = _handle(cfg, request)
        if response is not None:  # notifications get no response
            _send(response)
    return 0


def _handle(cfg: Config, request: dict) -> dict | None:
    method = request.get("method", "")
    request_id = request.get("id")

    if method == "initialize":
        return _result(request_id, {
            "protocolVersion": PROTOCOL_VERSION,
            "capabilities": {"tools": {}},
            "serverInfo": {"name": SERVER_NAME, "version": __version__},
        })
    if method in ("notifications/initialized", "initialized"):
        return None
    if method == "tools/list":
        return _result(request_id, {"tools": TOOLS})
    if method == "tools/call":
        params = request.get("params", {})
        name = params.get("name", "")
        arguments = params.get("arguments") or {}
        try:
            text = _call_tool(cfg, name, arguments)
            return _result(request_id, {
                "content": [{"type": "text", "text": text}],
                "isError": False,
            })
        except Exception as exc:
            # Message classes are safe (no transcript content in exceptions).
            return _result(request_id, {
                "content": [{"type": "text",
                             "text": f"error: {type(exc).__name__}: {exc}"}],
                "isError": True,
            })
    if request_id is None:
        return None
    return {"jsonrpc": "2.0", "id": request_id,
            "error": {"code": -32601, "message": f"method not found: {method}"}}


def _call_tool(cfg: Config, name: str, args: dict) -> str:
    if name == "connector_status":
        return _tool_status(cfg)

    db_path = cfg.resolve_db_path()
    with MeetilyDB(db_path) as db:
        try:
            if name == "list_meetings":
                return _tool_list(db, args)
            if name == "get_meeting":
                return _tool_get(db, args)
            if name == "get_transcript":
                return _tool_transcript(db, args)
            if name == "search_meetings":
                return _tool_search(db, args)
            if name == "get_summary":
                return _tool_summary(db, args)
        except DatabaseBusy:
            return "Meetily is writing to its database right now; retry in a few seconds."
    raise ValueError(f"unknown tool {name!r}")


def _tool_list(db: MeetilyDB, args: dict) -> str:
    rows = db.list_meetings(limit=args.get("limit", 20),
                            since=args.get("since"), until=args.get("until"))
    if not rows:
        return "No meetings found."
    lines = []
    for m in rows:
        status = m.summary_status or "no summary"
        lines.append(f"- {m.title} (id={m.id}, created={m.created_at}, summary={status})")
    return "\n".join(lines)


def _tool_get(db: MeetilyDB, args: dict) -> str:
    meeting = db.get_meeting(args["meeting_id"])
    if meeting is None:
        return f"No meeting with id {args['meeting_id']!r}."
    _, duration = db.get_transcript_text(meeting.id)
    lines = [
        f"title: {meeting.title}",
        f"id: {meeting.id}",
        f"created_at: {meeting.created_at}",
        f"duration_seconds: {round(duration)}",
        f"summary_status: {meeting.summary_status or 'no summary'}",
    ]
    if meeting.folder_path:
        lines.append(f"folder_path: {meeting.folder_path}")
    return "\n".join(lines)


def _tool_transcript(db: MeetilyDB, args: dict) -> str:
    text, _ = db.get_transcript_text(args["meeting_id"])
    if not text:
        return f"No transcript for meeting {args['meeting_id']!r}."
    offset = max(int(args.get("offset", 0)), 0)
    max_chars = min(int(args.get("max_chars", 50_000)), 200_000)
    window = text[offset:offset + max_chars]
    end = offset + len(window)
    if end < len(text):
        window += f"\n[TRUNCATED at char {end} of {len(text)}; call again with offset={end}]"
    return window


def _tool_search(db: MeetilyDB, args: dict) -> str:
    hits = db.search_meetings(args["query"], limit=args.get("limit", 10))
    if not hits:
        return f"No meetings match {args['query']!r}."
    blocks = []
    for h in hits:
        blocks.append(f"- {h['title']} (id={h['id']}, created={h['created_at']})\n"
                      f"  …{h['snippet']}")
    return "\n".join(blocks)


def _tool_summary(db: MeetilyDB, args: dict) -> str:
    meeting = db.get_meeting(args["meeting_id"])
    if meeting is None:
        return f"No meeting with id {args['meeting_id']!r}."
    if meeting.summary_status != "completed":
        return f"Summary status: {meeting.summary_status or 'never generated'}."
    if not meeting.summary_result:
        return "Summary is marked completed but has no stored result."
    try:
        return json.dumps(json.loads(meeting.summary_result),
                          ensure_ascii=False, indent=2)
    except ValueError:
        return meeting.summary_result


def _tool_status(cfg: Config) -> str:
    lines = [f"meetily-connector {__version__}"]
    try:
        db_path = cfg.resolve_db_path()
        lines.append(f"database: {db_path}")
        with MeetilyDB(db_path) as db:
            for key, value in db.counts().items():
                lines.append(f"  {key}: {value}")
            for warning in db.schema_warnings():
                lines.append(f"  WARNING: {warning}")
    except ConfigError as exc:
        lines.append(f"database: not found ({exc})")
    except DatabaseBusy:
        lines.append("database: busy (Meetily is writing)")

    state_db = cfg.state_dir / "state.db"
    if state_db.exists():
        from .outbox import StateStore

        store = StateStore(cfg)
        try:
            lines.append(f"outbox_depth: {store.depth()}")
        finally:
            store.close()
    else:
        lines.append("watch_mode: never ran")
    return "\n".join(lines)


def _result(request_id, result: dict) -> dict:
    return {"jsonrpc": "2.0", "id": request_id, "result": result}


def _send(obj: dict) -> None:
    sys.stdout.write(json.dumps(obj, ensure_ascii=False) + "\n")
    sys.stdout.flush()


def _send_error(request_id, code: int, message: str) -> None:
    _send({"jsonrpc": "2.0", "id": request_id,
           "error": {"code": code, "message": message}})
