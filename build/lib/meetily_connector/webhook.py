"""Webhook delivery: HMAC-V2 signed POST via urllib.

Signature contract (verified against the Hermes generic webhook adapter, and
generic enough for any receiver):
- sign the EXACT bytes transmitted: hmac_sha256(secret, b"<ts>." + body)
- X-Webhook-Timestamp carries the same timestamp string used in the signature
  (unix seconds; receivers typically enforce a +/-300s replay window)
- X-Webhook-Signature-V2 is lowercase hex, no "sha256=" prefix
- X-Request-ID is a per-delivery UUID (receiver-side idempotency key)
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
import urllib.error
import urllib.request
import uuid
from dataclasses import dataclass

from .config import check_transport


@dataclass
class DeliveryResult:
    ok: bool
    http_status: int | None  # None for transport-level failures
    error: str  # short class/status description; never response bodies
    payload_sha256: str = ""


def deliver(url: str, secret: str, payload: dict,
            allow_insecure: bool, timeout: int = 30) -> DeliveryResult:
    # Re-checked per request so a config reload can't downgrade transport safety.
    check_transport(url, allow_insecure)

    # Serialize once; these exact bytes are both signed and transmitted.
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    ts = str(int(time.time()))
    signature = hmac.new(secret.encode(), ts.encode() + b"." + body,
                         hashlib.sha256).hexdigest()
    request = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "X-Webhook-Signature-V2": signature,
            "X-Webhook-Timestamp": ts,
            "X-Request-ID": str(uuid.uuid4()),
            "User-Agent": "meetily-connector",
        },
    )
    sha = hashlib.sha256(body).hexdigest()
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            return DeliveryResult(True, response.status, "", sha)
    except urllib.error.HTTPError as exc:
        return DeliveryResult(False, exc.code, f"http_{exc.code}", sha)
    except urllib.error.URLError as exc:
        reason = type(exc.reason).__name__ if exc.reason else "URLError"
        return DeliveryResult(False, None, f"transport_{reason}", sha)
    except TimeoutError:
        return DeliveryResult(False, None, "transport_Timeout", sha)
