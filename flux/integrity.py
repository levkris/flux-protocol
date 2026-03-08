import hashlib
import json
import logging
import time
from typing import Optional

from .crypto import verify, b64d
from .constants import TRUST_THRESHOLD
from .identity import FluxIdentity

log = logging.getLogger("flux.integrity")


_reputation: dict[str, int] = {}
_quarantined: set[str] = set()
_server_identity: Optional[FluxIdentity] = None


def get_server_identity() -> FluxIdentity:
    global _server_identity
    if _server_identity is None:
        _server_identity = FluxIdentity.generate()
        log.info(f"generated ephemeral server identity: {_server_identity.address}")
    return _server_identity


def set_server_identity(identity: FluxIdentity):
    global _server_identity
    _server_identity = identity


def _integrity_payload(msg: dict) -> str:
    EXCLUDE = {"sig", "pub", "route", "integrity_chain", "_status", "_inbox", "_tags", "bcc"}
    core = {k: v for k, v in msg.items() if k not in EXCLUDE}
    return json.dumps(core, separators=(",", ":"), sort_keys=True)


def compute_hash(msg: dict) -> str:
    return hashlib.sha256(_integrity_payload(msg).encode()).hexdigest()


def make_hop_record(msg: dict, server_domain: str) -> dict:
    identity = get_server_identity()
    t = int(time.time() * 1000)
    h = compute_hash(msg)
    return {
        "server": server_domain,
        "t": t,
        "hash": h,
        "sig": identity.sign(f"{server_domain}:{t}:{h}"),
        "pub": identity.pub_b64(),
    }


def append_integrity_hop(msg: dict, server_domain: str) -> dict:
    m = dict(msg)
    chain = list(msg.get("integrity_chain") or [])
    chain.append(make_hop_record(msg, server_domain))
    m["integrity_chain"] = chain
    return m


def verify_integrity_chain(msg: dict) -> tuple[bool, Optional[str]]:
    """Returns (ok, offending_server_or_None)."""
    chain = msg.get("integrity_chain") or []
    if not chain:
        return True, None

    expected_hash = compute_hash({**msg, "integrity_chain": []})

    for hop in chain:
        server = hop.get("server", "?")
        t = hop.get("t", 0)
        h = hop.get("hash", "")

        try:
            pub_bytes = b64d(hop.get("pub", ""))
            if not verify(pub_bytes, f"{server}:{t}:{h}", hop.get("sig", "")):
                log.warning(f"integrity: bad signature from {server}")
                return False, server
        except Exception as e:
            log.warning(f"integrity: sig verify error from {server}: {e}")
            return False, server

        if h != expected_hash:
            log.warning(f"integrity: hash mismatch at {server} — expected {expected_hash[:12]}… got {h[:12]}…")
            return False, server

    return True, None


def record_tamper(offender: str) -> int:
    _reputation[offender] = _reputation.get(offender, 0) + 1
    count = _reputation[offender]
    if count >= TRUST_THRESHOLD:
        _quarantined.add(offender)
        log.warning(f"QUARANTINED: {offender} (strikes={count})")
    else:
        log.warning(f"tamper strike {count}/{TRUST_THRESHOLD} for {offender}")
    return count


def is_quarantined(server: str) -> bool:
    return server in _quarantined


def get_reputation() -> dict:
    return {"strikes": dict(_reputation), "quarantined": sorted(_quarantined)}


def trust_score(server: str) -> int:
    return _reputation.get(server, 0)


def build_tamper_report(msg: dict, offender: str, reporter: str) -> dict:
    identity = get_server_identity()
    t = int(time.time() * 1000)
    payload = f"tamper:{msg.get('id','?')}:{offender}:{reporter}:{t}"
    return {
        "type": "tamper_report",
        "msg_id": msg.get("id", ""),
        "offender": offender,
        "reporter": reporter,
        "t": t,
        "sig": identity.sign(payload),
        "pub": identity.pub_b64(),
        "integrity_chain": msg.get("integrity_chain", []),
        "msg_hash_baseline": compute_hash({**msg, "integrity_chain": []}),
    }


def verify_tamper_report(report: dict) -> bool:
    """Verify the tamper report signature only."""
    try:
        pub_bytes = b64d(report["pub"])
        payload = f"tamper:{report['msg_id']}:{report['offender']}:{report['reporter']}:{report['t']}"
        return verify(pub_bytes, payload, report["sig"])
    except Exception:
        return False


def validate_tamper_report(report: dict) -> bool:
    """
    Verify that the tamper report is legitimate by:
    1. Checking the report signature
    2. Verifying the integrity chain shows actual tampering by the claimed offender
    """
    if not verify_tamper_report(report):
        return False
    
    chain = report.get("integrity_chain", [])
    baseline = report.get("msg_hash_baseline", "")
    offender = report.get("offender", "")
    
    if not chain or not baseline or not offender:
        return False
    
    for hop in chain:
        server = hop.get("server", "")
        h = hop.get("hash", "")
        
        try:
            pub_bytes = b64d(hop.get("pub", ""))
            if not verify(pub_bytes, f"{server}:{hop.get('t', 0)}:{h}", hop.get("sig", "")):
                return False
        except Exception:
            return False
        
        if server == offender and h != baseline:
            return True
    
    return False