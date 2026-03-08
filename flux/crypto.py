import hashlib
import base64

from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

from .constants import ADDRESS_PREFIX, ADDRESS_HASH_LEN


def b64e(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).decode()


def b64d(s: str) -> bytes:
    return base64.urlsafe_b64decode(s.encode())


def pub_to_address(pub_bytes: bytes) -> str:
    return ADDRESS_PREFIX + hashlib.sha256(pub_bytes).hexdigest()[:ADDRESS_HASH_LEN]


def generate_keypair() -> tuple[Ed25519PrivateKey, Ed25519PublicKey]:
    priv = Ed25519PrivateKey.generate()
    return priv, priv.public_key()


def private_key_to_bytes(priv: Ed25519PrivateKey) -> bytes:
    return priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption()
    )


def public_key_to_bytes(pub: Ed25519PublicKey) -> bytes:
    return pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)


def private_key_from_bytes(raw: bytes) -> Ed25519PrivateKey:
    return Ed25519PrivateKey.from_private_bytes(raw)


def sign(priv: Ed25519PrivateKey, payload: str) -> str:
    return b64e(priv.sign(payload.encode()))


def verify(pub_bytes: bytes, payload: str, sig: str) -> bool:
    try:
        pub = Ed25519PublicKey.from_public_bytes(pub_bytes)
        pub.verify(b64d(sig), payload.encode())
        return True
    except (InvalidSignature, Exception):
        return False
