import json
from pathlib import Path

from .crypto import (
    generate_keypair, private_key_from_bytes, private_key_to_bytes,
    public_key_to_bytes, pub_to_address, b64e, b64d, sign
)


class FluxIdentity:
    """Represents a FLUX user. The address is derived from the public key — no registration needed."""

    def __init__(self, priv, pub):
        self._priv = priv
        self._pub = pub
        self._pub_bytes = public_key_to_bytes(pub)
        self.address = pub_to_address(self._pub_bytes)

    @classmethod
    def generate(cls) -> "FluxIdentity":
        priv, pub = generate_keypair()
        return cls(priv, pub)

    @classmethod
    def from_private_b64(cls, encoded: str) -> "FluxIdentity":
        raw = b64d(encoded)
        priv = private_key_from_bytes(raw)
        return cls(priv, priv.public_key())

    @classmethod
    def from_file(cls, path: str | Path) -> "FluxIdentity":
        data = json.loads(Path(path).read_text())
        return cls.from_private_b64(data["private_key"])

    def save(self, path: str | Path):
        Path(path).write_text(json.dumps({
            "address": self.address,
            "private_key": self.export_private()
        }, indent=2))

    def export_private(self) -> str:
        return b64e(private_key_to_bytes(self._priv))

    def pub_b64(self) -> str:
        return b64e(self._pub_bytes)

    def pub_bytes(self) -> bytes:
        return self._pub_bytes

    def sign(self, payload: str) -> str:
        return sign(self._priv, payload)

    def __repr__(self) -> str:
        return f"FluxIdentity({self.address})"
