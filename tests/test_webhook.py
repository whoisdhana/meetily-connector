"""Signature + transport gate tests.

The oracle below reimplements the receiver-side V2 validation exactly as the
Hermes generic webhook adapter does (gateway/platforms/webhook.py:1150-1175):
signed_content = ts_header_bytes + b"." + raw_body; lowercase hexdigest;
constant-time compare; ±300s window on the timestamp header.
"""

import hashlib
import hmac
import json
import time
from unittest import mock

import pytest

from meetily_connector.config import ConfigError, check_transport
from meetily_connector.webhook import deliver


def _oracle_validate(headers: dict, body: bytes, secret: str) -> bool:
    v2_sig = headers.get("X-webhook-signature-v2", "") or headers.get("X-Webhook-Signature-V2", "")
    ts_header = headers.get("X-webhook-timestamp", "") or headers.get("X-Webhook-Timestamp", "")
    if not v2_sig or not ts_header:
        return False
    try:
        ts = int(ts_header)
    except (TypeError, ValueError):
        return False
    if abs(int(time.time()) - ts) > 300:
        return False
    signed_content = ts_header.encode() + b"." + body
    expected = hmac.new(secret.encode(), signed_content, hashlib.sha256).hexdigest()
    return hmac.compare_digest(v2_sig.encode(), expected.encode())


def test_signature_matches_receiver_oracle():
    captured = {}

    def fake_urlopen(request, timeout=0):
        captured["headers"] = dict(request.headers)
        captured["body"] = request.data

        class R:
            status = 202

            def __enter__(self):
                return self

            def __exit__(self, *a):
                pass

        return R()

    payload = {"event": "meeting.completed",
               "transcript": {"text": "வணக்கம் team, deployment update"}}
    with mock.patch("urllib.request.urlopen", fake_urlopen):
        result = deliver("http://127.0.0.1:8644/webhooks/x", "s3cret",
                         payload, allow_insecure=False)
    assert result.ok and result.http_status == 202
    assert _oracle_validate(captured["headers"], captured["body"], "s3cret")
    assert not _oracle_validate(captured["headers"], captured["body"], "wrong")
    # signature is lowercase hex, no prefix
    sig = captured["headers"].get("X-webhook-signature-v2")
    assert sig and sig == sig.lower() and not sig.startswith("sha256=")
    # body bytes are exactly what was signed — and valid JSON
    assert json.loads(captured["body"])["event"] == "meeting.completed"
    # idempotency key present
    assert captured["headers"].get("X-request-id")


def test_transport_gate_matrix():
    check_transport("https://example.com/hook", False)  # https anywhere: ok
    check_transport("http://127.0.0.1:8644/hook", False)  # loopback http: ok
    check_transport("http://localhost:8644/hook", False)
    check_transport("http://[::1]:8644/hook", False)
    with pytest.raises(ConfigError):
        check_transport("http://192.168.1.10/hook", False)
    with pytest.raises(ConfigError):
        check_transport("http://evil.example.com/hook", False)
    check_transport("http://192.168.1.10/hook", True)  # explicit opt-in
    with pytest.raises(ConfigError):
        check_transport("ftp://example.com/hook", False)


def test_connection_refused_reported_as_transport_error():
    result = deliver("http://127.0.0.1:9/webhooks/x", "s", {"a": 1},
                     allow_insecure=False, timeout=2)
    assert not result.ok
    assert result.http_status is None
    assert result.error.startswith("transport_")
