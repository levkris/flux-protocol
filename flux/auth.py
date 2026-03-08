import hashlib
import hmac
import os


def derive_token(address: str, secret: str | None = None) -> str:
    """
    Derive a deterministic fetch token for an address.
    The secret should be set via FLUX_SECRET environment variable in production.
    """
    key = secret or os.environ.get("FLUX_SECRET", "insecure-default-change-me")
    return hashlib.sha256(f"flux:{address}:{key}".encode()).hexdigest()


def validate_token(address: str, token: str, secret: str | None = None) -> bool:
    if not address or not token:
        return False
    expected = derive_token(address, secret)
    return hmac.compare_digest(token, expected)
