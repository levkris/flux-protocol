import json
import time
import uuid
from typing import Optional

from .constants import FLUX_VERSION, MAX_CONTENT_BYTES, MAX_MESSAGE_AGE_MS
from .crypto import b64d, pub_to_address, verify
from .identity import FluxIdentity


def now_ms() -> int:
    return int(time.time() * 1000)


def make_id() -> str:
    return uuid.uuid4().hex


def build_message(identity: FluxIdentity, to: str, content: str, reply_to: Optional[str] = None) -> dict:
    """Construct and sign a FLUX message envelope."""
    if len(content.encode()) > MAX_CONTENT_BYTES:
        raise ValueError(f"Content exceeds {MAX_CONTENT_BYTES} bytes")

    envelope = {
        "v": FLUX_VERSION,
        "id": make_id(),
        "from": identity.address,
        "to": to,
        "t": now_ms(),
        "content": content,
    }

    if reply_to:
        envelope["re"] = reply_to

    # Sign only the core fields — pub and sig are added after
    payload = json.dumps(envelope, separators=(",", ":"), sort_keys=True)
    envelope["sig"] = identity.sign(payload)
    envelope["pub"] = identity.pub_b64()

    return envelope


def verify_message(msg: dict) -> bool:
    """
    Verify that:
    1. The public key hashes to the claimed address
    2. The signature is valid over the core fields
    """
    try:
        pub_bytes = b64d(msg["pub"])

        if pub_to_address(pub_bytes) != msg["from"]:
            return False

        core = {k: v for k, v in msg.items() if k not in ("sig", "pub")}
        payload = json.dumps(core, separators=(",", ":"), sort_keys=True)

        return verify(pub_bytes, payload, msg["sig"])
    except Exception:
        return False


def check_freshness(msg: dict) -> bool:
    """Reject replayed or clock-skewed messages."""
    return abs(now_ms() - msg.get("t", 0)) <= MAX_MESSAGE_AGE_MS


REQUIRED_FIELDS = ("v", "id", "from", "to", "t", "content", "sig", "pub")


def validate_fields(msg: dict) -> bool:
    return all(k in msg for k in REQUIRED_FIELDS)
