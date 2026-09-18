"""Checking that a delivery is ours.

Your webhook URL is public: anybody who learns it can POST to it, and a handler that acts
on whatever arrives is a way to write into your moderation queue from outside. This is the
only thing standing between those two facts, which is why it ships in the client rather
than as a paragraph everybody implements slightly differently.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import time
from typing import Any

HEADER = "X-ToxicFilter-Signature"


def verify(payload: bytes | str, header: str, secret: str, tolerance: int = 300) -> bool:
    """Whether ``header`` signs ``payload``, recently, with ``secret``.

    Pass the RAW body. Re-encoding what a framework parsed can reorder a key or escape a
    slash differently, and then the signature over "the body" is a signature over a
    different string.
    """
    if isinstance(payload, str):
        payload = payload.encode()

    parts: dict[str, str] = {}

    for piece in header.split(","):
        name, _, value = piece.strip().partition("=")
        parts[name] = value

    try:
        timestamp = int(parts.get("t", "0"))
    except ValueError:
        return False

    signature = parts.get("v1", "")

    if timestamp <= 0 or not signature:
        return False

    # The timestamp is signed WITH the body, so a delivery captured today cannot be
    # replayed tomorrow: moving it breaks the signature and leaving it stale puts the
    # request outside this window.
    if abs(int(time.time()) - timestamp) > tolerance:
        return False

    expected = hmac.new(
        secret.encode(),
        f"{timestamp}.".encode() + payload,
        hashlib.sha256,
    ).hexdigest()

    # Constant time: a comparison that returns early tells an attacker how much of their
    # guess was right.
    return hmac.compare_digest(expected, signature)


def event(
    payload: bytes | str,
    header: str,
    secret: str,
    tolerance: int = 300,
) -> dict[str, Any] | None:
    """Decode the event, or return None when it does not verify."""
    if not verify(payload, header, secret, tolerance):
        return None

    if isinstance(payload, bytes):
        payload = payload.decode()

    try:
        decoded = json.loads(payload)
    except ValueError:
        return None

    return decoded if isinstance(decoded, dict) else None
