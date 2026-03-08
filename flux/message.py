import json
import time
import uuid
from typing import Optional

from .constants import FLUX_VERSION, MAX_CONTENT_BYTES, MAX_MESSAGE_AGE_MS, TAG_IMPORTANT, TAG_FAVORITED, RESERVED_TAGS
from .crypto import b64d, pub_to_address, verify
from .identity import FluxIdentity

def now_ms() -> int:
    return int(time.time() * 1000)
def make_id() -> str:
    return uuid.uuid4().hex
def build_message(
    identity: FluxIdentity,
    to: str,
    content: str,
    subject: Optional[str] = None,
    reply_to: Optional[str] = None,
    cc: Optional[list[str]] = None,
    bcc: Optional[list[str]] = None,
    tags: Optional[list[str]] = None,
    expires: Optional[int] = None,
) -> dict:
    if len(content.encode()) > MAX_CONTENT_BYTES:
        raise ValueError(f"Content exceeds {MAX_CONTENT_BYTES} bytes")

    envelope: dict = {
        "v": FLUX_VERSION,
        "id": make_id(),
        "from": identity.address,
        "to": to,
        "t": now_ms(),
        "content": content,
    }

    if subject:
        envelope["subject"] = subject
    if reply_to:
        envelope["re"] = reply_to
    if cc:
        envelope["cc"] = [a for a in cc if a != to]
    if bcc:
        envelope["bcc"] = bcc
    if tags:
        envelope["tags"] = list({t.lower() for t in tags})
    if expires is not None:
        envelope["expires"] = expires

    # route and integrity_chain are appended by servers and excluded from the sender's signature
    envelope["route"] = []
    envelope["integrity_chain"] = []

    core = _core_fields(envelope)
    payload = json.dumps(core, separators=(",", ":"), sort_keys=True)
    envelope["sig"] = identity.sign(payload)
    envelope["pub"] = identity.pub_b64()

    return envelope
def _core_fields(msg: dict) -> dict:
    EXCLUDE = {"sig", "pub", "route", "integrity_chain", "_status", "_inbox", "_tags"}
    return {k: v for k, v in msg.items() if k not in EXCLUDE}
def strip_bcc(msg: dict) -> dict:
    return {k: v for k, v in msg.items() if k != "bcc"}
def append_route(msg: dict, hop: str) -> dict:
    m = dict(msg)
    m["route"] = list(msg.get("route") or []) + [{"server": hop, "t": now_ms()}]
    return m
def verify_message(msg: dict) -> bool:
    try:
        pub_bytes = b64d(msg["pub"])
        if pub_to_address(pub_bytes) != msg["from"]:
            return False
        core = _core_fields(msg)
        payload = json.dumps(core, separators=(",", ":"), sort_keys=True)
        return verify(pub_bytes, payload, msg["sig"])
    except Exception:
        return False
def check_freshness(msg: dict) -> bool:
    return abs(now_ms() - msg.get("t", 0)) <= MAX_MESSAGE_AGE_MS
def is_expired(msg: dict) -> bool:
    exp = msg.get("expires")
    if exp is None:
        return False
    return now_ms() >= exp
REQUIRED_FIELDS = ("v", "id", "from", "to", "t", "content", "sig", "pub")
def validate_fields(msg: dict) -> bool:
    return all(k in msg for k in REQUIRED_FIELDS)