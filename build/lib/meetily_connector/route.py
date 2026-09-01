"""Hermes webhook route definition + safe merge into webhook_subscriptions.json.

This module is the single source of truth for the meeting-ingestion route,
including the extraction prompt. The installer merges the route into a box's
existing subscriptions file WITHOUT touching other routes, because the
`hermes webhook subscribe` CLI rotates omitted secrets and drops hand-added
keys like `toolsets` (verified against Hermes v0.20.6).

Usage (from setup.sh):
    existing_json | python -m meetily_connector.route merge --secret-fd 0 ...
The secret arrives on a dedicated line of stdin — never argv, never env.
"""

from __future__ import annotations

import json
import sys
from datetime import datetime, timezone

ROUTE_NAME = "meetily-meeting"

ROUTE_PROMPT = """\
A Meetily meeting recording just finished.

Meeting: {meeting.title} (id {meeting.id}), started {meeting.created_at}, duration {meeting.duration_seconds}s.
Transcript (source={transcript.source}, truncated={transcript.truncated}):
{transcript.text}

The transcript may be code-mixed (e.g. Tanglish: Tamil-English). IMPORTANT: English technical terms often appear TRANSLITERATED INTO THE LOCAL SCRIPT - e.g. வெப்சைட் = website, ப்ரைசிங் = pricing, டிசைன் = design. This is normal ASR behavior, NOT garbling. Mentally convert such words back to English before judging transcript quality.

Do the following, in order:

1. NORMALIZE: read the transcript converting transliterated technical terms back to English. Keep proper nouns and product names in Latin script in your output.
2. SUMMARIZE in English: 3-10 bullets covering topics, decisions, blockers, deadlines. Even a short status update deserves a summary.
3. EXTRACT ACTION ITEMS: only items with a clear task. Owner = person named in the meeting, else "unassigned". Resolve relative dates ("next Friday") to absolute dates from the meeting start time, using the meeting's local timezone.
4. CREATE TASKS: for each action item, call hermesnag create_task with: title = the action; notes = context + owner; source_ref = "meetily:{meeting.id}"; due_at as timezone-aware ISO-8601 or omitted; priority "normal" ("high" only if the meeting flagged urgency) - NEVER "must". First list existing tasks and skip any you already created for this source_ref.
5. WHEN NOT TO CREATE TASKS:
   - Meeting has content but no explicit action items: deliver the summary only. Do NOT create tasks and do NOT create a review task.
   - Transcript truly unintelligible EVEN AFTER converting transliterated terms (random syllables, no coherent meaning): create exactly one task "Review meeting: {meeting.title}" explaining why, and say so in your reply.
6. REPLY with the English summary and any tasks created. Only reply [SILENT] for empty or accidental recordings with no meaningful content at all.
"""


def build_route(secret: str) -> dict:
    return {
        "description": "Meetily meeting.completed ingestion (installed by meetily-connector setup)",
        "events": [],
        "secret": secret,
        "prompt": ROUTE_PROMPT,
        "skills": [],
        "deliver": "telegram",
        "toolsets": ["hermesnag"],
        "created_at": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }


def merge_route(existing: dict, secret: str) -> tuple[dict, str]:
    """Return (merged subscriptions dict, human summary with secret redacted)."""
    if not isinstance(existing, dict):
        raise ValueError("existing subscriptions must be a JSON object")
    merged = dict(existing)
    previous = merged.get(ROUTE_NAME)
    route = build_route(secret)
    if previous:
        # Keep the original creation time; note what changes.
        route["created_at"] = previous.get("created_at", route["created_at"])
    merged[ROUTE_NAME] = route

    other = sorted(k for k in merged if k != ROUTE_NAME)
    lines = [
        f"route: {ROUTE_NAME} ({'UPDATED' if previous else 'NEW'})",
        f"  deliver: {route['deliver']}  toolsets: {route['toolsets']}",
        f"  secret: (redacted, {len(secret)} chars, from macOS Keychain)",
        f"  prompt: {len(route['prompt'])} chars",
        f"other routes preserved untouched: {other if other else '(none)'}",
    ]
    if previous:
        changed = [k for k in route
                   if k != "secret" and previous.get(k) != route.get(k)]
        same_secret = previous.get("secret") == secret
        lines.insert(1, f"  changed fields: {changed or '(none)'}"
                        f"  secret: {'unchanged' if same_secret else 'REPLACED'}")
    return merged, "\n".join(lines)


def main(argv: list[str] | None = None) -> int:
    args = argv if argv is not None else sys.argv[1:]
    if args != ["merge"]:  # exactly; the secret must never appear in argv
        print("usage: python -m meetily_connector.route merge < input", file=sys.stderr)
        print("  stdin line 1: the HMAC secret", file=sys.stderr)
        print("  stdin rest:   existing subscriptions JSON (or empty for {})", file=sys.stderr)
        return 2
    secret = sys.stdin.readline().strip()
    if not secret:
        print("error: no secret on stdin line 1", file=sys.stderr)
        return 2
    rest = sys.stdin.read().strip()
    existing = json.loads(rest) if rest else {}
    merged, summary = merge_route(existing, secret)
    print(summary, file=sys.stderr)
    json.dump(merged, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


if __name__ == "__main__":
    sys.exit(main())
