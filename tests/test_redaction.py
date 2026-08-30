"""Redaction guarantees: transcript text and secrets never reach the logger."""

import logging
import re
from pathlib import Path

from meetily_connector.logging_setup import log_event

SRC = Path(__file__).resolve().parents[1] / "src" / "meetily_connector"


def test_log_event_caps_and_flattens(caplog):
    with caplog.at_level(logging.INFO, logger="meetily_connector"):
        log_event("x", huge="A" * 10_000, obj={"transcript": "secret text"})
    record = caplog.records[-1].getMessage()
    assert len(record) < 600
    assert "secret text" not in record  # dicts are flattened to a type tag
    assert "<dict>" in record


def test_no_module_logs_content_fields():
    """Only logging_setup touches the logging module; everyone else must go
    through log_event(), whose signature admits scalars only."""
    pattern = re.compile(r"logging\.(getLogger|info|warning|error|debug)")
    offenders = []
    for path in SRC.glob("*.py"):
        if path.name == "logging_setup.py":
            continue
        if pattern.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert offenders == []


def test_no_personal_endpoints_in_source():
    """Repo hygiene: personal deployment specifics stay in docs, not code."""
    banned = re.compile(r"8787|hermesnag-token|152\.67\.|dhanapale", re.IGNORECASE)
    offenders = []
    for path in SRC.glob("*.py"):
        if banned.search(path.read_text(encoding="utf-8")):
            offenders.append(path.name)
    assert offenders == []
