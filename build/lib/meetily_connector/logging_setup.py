"""Logging with a redaction guarantee.

All operational logging goes through log_event(), which accepts only scalar
metadata (ids, sizes, statuses). Transcript text, payload bodies, and secrets
never reach the logger — enforced by convention here and by a grep-test in CI
asserting no other module calls logging directly with content fields.

serve mode: stdout is reserved for JSON-RPC; logs go to stderr.
watch mode: logs go to stderr (launchd redirects to the log file).
"""

from __future__ import annotations

import logging
import sys

_LOGGER_NAME = "meetily_connector"

_ALLOWED_VALUE_TYPES = (str, int, float, bool, type(None))
_MAX_VALUE_CHARS = 200  # backstop: nothing transcript-sized fits through


def setup_logging(verbose: bool = False) -> logging.Logger:
    logger = logging.getLogger(_LOGGER_NAME)
    if logger.handlers:
        return logger
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(
        logging.Formatter("%(asctime)s %(levelname)s %(message)s", "%Y-%m-%dT%H:%M:%S%z")
    )
    logger.addHandler(handler)
    logger.setLevel(logging.DEBUG if verbose else logging.INFO)
    return logger


def log_event(event: str, level: int = logging.INFO, **meta: object) -> None:
    """Log a structured event. Values are scalars only and length-capped."""
    logger = logging.getLogger(_LOGGER_NAME)
    parts = [event]
    for key, value in meta.items():
        if not isinstance(value, _ALLOWED_VALUE_TYPES):
            value = f"<{type(value).__name__}>"
        text = str(value)
        if len(text) > _MAX_VALUE_CHARS:
            text = text[:_MAX_VALUE_CHARS] + "…"
        parts.append(f"{key}={text}")
    logger.log(level, " ".join(parts))
