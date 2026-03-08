import hashlib
import os
from typing import Optional

from cryptography.hazmat.primitives.asymmetric.x25519 import X25519PrivateKey, X25519PublicKey
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey, Ed25519PublicKey
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from cryptography.hazmat.primitives import serialization

from .crypto import b64e, b64d
from .identity import FluxIdentity

log = __import__("logging").getLogger("flux.encryption")


# Ed25519 → X25519 conversion via the standard birational map,
# so we can reuse the existing identity keypair for ECDH without a separate encryption key.

def _ed25519_priv_to_x25519(ed_priv: Ed25519PrivateKey) -> X25519PrivateKey:
    raw = ed_priv.private_bytes(
        serialization.Encoding.Raw,
        serialization.PrivateFormat.Raw,
        serialization.NoEncryption(),
    )
    # RFC 8032 §5.1.5 scalar clamping
    h = hashlib.sha512(raw).digest()
    scalar = bytearray(h[:32])
    scalar[0] &= 248
    scalar[31] &= 127
    scalar[31] |= 64
    return X25519PrivateKey.from_private_bytes(bytes(scalar))


def _ed25519_pub_to_x25519(ed_pub: Ed25519PublicKey) -> X25519PublicKey:
    raw = ed_pub.public_bytes(serialization.Encoding.Raw, serialization.PublicFormat.Raw)
    # u = (1+y)/(1-y) mod p - birational map from Edwards to Montgomery curve
    y_bytes = bytearray(raw)
    y_bytes[31] &= 0x7F  # strip sign bit
    y = int.from_bytes(y_bytes, "little")
    P = 2**255 - 19
    u = (1 + y) * pow(1 - y, P - 2, P) % P
    return X25519PublicKey.from_public_bytes(u.to_bytes(32, "little"))


def _aes_encrypt(key: bytes, plaintext: bytes) -> bytes:
    """Returns nonce(12) + ciphertext + GCM tag(16)."""
    nonce = os.urandom(12)
    return nonce + AESGCM(key).encrypt(nonce, plaintext, None)


def _aes_decrypt(key: bytes, blob: bytes) -> bytes:
    return AESGCM(key).decrypt(blob[:12], blob[12:], None)


def encrypt_message(msg: dict, recipient_pub_b64s: dict[str, str]) -> dict:
    """
    Encrypt a message for one or more recipients.
    recipient_pub_b64s: {fx1_address: base64url_ed25519_pub}
    """
    cek = os.urandom(32)
    content_enc = b64e(_aes_encrypt(cek, msg["content"].encode()))

    enc_recipients: dict[str, str] = {}
    for fx1_addr, pub_b64 in recipient_pub_b64s.items():
        try:
            ed_pub = Ed25519PublicKey.from_public_bytes(b64d(pub_b64))
            eph_priv = X25519PrivateKey.generate()
            eph_pub_bytes = eph_priv.public_key().public_bytes(
                serialization.Encoding.Raw, serialization.PublicFormat.Raw
            )
            shared = eph_priv.exchange(_ed25519_pub_to_x25519(ed_pub))
            wrap_key = hashlib.sha256(shared).digest()
            # Layout: eph_pub(32) || aes_encrypt(wrap_key, cek)
            enc_recipients[fx1_addr] = b64e(eph_pub_bytes + _aes_encrypt(wrap_key, cek))
        except Exception as e:
            log.warning(f"could not encrypt for {fx1_addr}: {e}")

    return {**msg, "encrypted": True, "content": "[encrypted]",
            "content_enc": content_enc, "enc_recipients": enc_recipients}


def decrypt_message(msg: dict, identity: FluxIdentity) -> Optional[str]:
    """Returns plaintext, or None if not a recipient or decryption fails."""
    if not msg.get("encrypted"):
        return msg.get("content")

    wrapped = (msg.get("enc_recipients") or {}).get(identity.address)
    if not wrapped:
        return None

    try:
        blob = b64d(wrapped)
        shared = _ed25519_priv_to_x25519(identity._priv).exchange(
            X25519PublicKey.from_public_bytes(blob[:32])
        )
        wrap_key = hashlib.sha256(shared).digest()
        cek = _aes_decrypt(wrap_key, blob[32:])
        return _aes_decrypt(cek, b64d(msg["content_enc"])).decode()
    except Exception as e:
        log.warning(f"decryption failed: {e}")
        return None


def is_encrypted(msg: dict) -> bool:
    return bool(msg.get("encrypted"))