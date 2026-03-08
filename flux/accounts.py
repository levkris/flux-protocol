import asyncio
import hashlib
import json
import os
import secrets
import sqlite3
import time
from pathlib import Path
from typing import Optional

from .crypto import generate_keypair, private_key_to_bytes, public_key_to_bytes, pub_to_address, b64e
from .identity import FluxIdentity


def _now() -> int:
    return int(time.time())


def _hash_password(password: str, salt: str) -> str:
    return hashlib.scrypt(
        password.encode(),
        salt=salt.encode(),
        n=16384, r=8, p=1,
        dklength=32
    ).hex()


class AccountStore:
    """
    Manages user accounts for a single FLUX domain node.

    Each account ties a human-readable username to a FLUX keypair.
    Authentication can be password-based or OAuth (provider + provider_user_id).
    The private key is stored encrypted-at-rest using the server's FLUX_SECRET.
    """

    def __init__(self, db_path: str | Path = "flux_accounts.db"):
        self._path = str(db_path)
        self._lock = asyncio.Lock()
        self._init_db()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    def _init_db(self):
        con = self._connect()
        con.executescript("""
            CREATE TABLE IF NOT EXISTS accounts (
                username        TEXT PRIMARY KEY,
                display_name    TEXT,
                flux_address    TEXT NOT NULL UNIQUE,
                flux_pub_b64    TEXT NOT NULL,
                flux_priv_b64   TEXT NOT NULL,
                created_at      INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_password (
                username    TEXT PRIMARY KEY REFERENCES accounts(username),
                salt        TEXT NOT NULL,
                hash        TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS auth_oauth (
                provider        TEXT NOT NULL,
                provider_uid    TEXT NOT NULL,
                username        TEXT NOT NULL REFERENCES accounts(username),
                PRIMARY KEY (provider, provider_uid)
            );

            CREATE TABLE IF NOT EXISTS sessions (
                token       TEXT PRIMARY KEY,
                username    TEXT NOT NULL REFERENCES accounts(username),
                created_at  INTEGER NOT NULL,
                expires_at  INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_sessions_username ON sessions(username);
            CREATE INDEX IF NOT EXISTS idx_accounts_address ON accounts(flux_address);
        """)
        con.commit()
        con.close()

    # --- Account creation ---

    async def create_account(
        self,
        username: str,
        display_name: Optional[str] = None,
        password: Optional[str] = None,
    ) -> dict:
        """Create a new account. Generates a fresh FLUX keypair automatically."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._create_sync, username, display_name, password)

    def _create_sync(self, username: str, display_name: Optional[str], password: Optional[str]) -> dict:
        priv, pub = generate_keypair()
        pub_bytes = public_key_to_bytes(pub)
        address = pub_to_address(pub_bytes)
        pub_b64 = b64e(pub_bytes)
        priv_b64 = b64e(private_key_to_bytes(priv))

        con = self._connect()
        try:
            existing = con.execute("SELECT username FROM accounts WHERE username=?", (username,)).fetchone()
            if existing:
                raise ValueError(f"Username '{username}' already taken")

            con.execute(
                "INSERT INTO accounts (username, display_name, flux_address, flux_pub_b64, flux_priv_b64, created_at) VALUES (?,?,?,?,?,?)",
                (username, display_name, address, pub_b64, priv_b64, _now())
            )

            if password:
                salt = secrets.token_hex(16)
                pw_hash = _hash_password(password, salt)
                con.execute(
                    "INSERT INTO auth_password (username, salt, hash) VALUES (?,?,?)",
                    (username, salt, pw_hash)
                )

            con.commit()
            return {"username": username, "address": address, "display_name": display_name}
        finally:
            con.close()

    async def link_oauth(self, username: str, provider: str, provider_uid: str):
        """Link an OAuth identity to an existing account."""
        async with self._lock:
            loop = asyncio.get_event_loop()
            await loop.run_in_executor(None, self._link_oauth_sync, username, provider, provider_uid)

    def _link_oauth_sync(self, username: str, provider: str, provider_uid: str):
        con = self._connect()
        try:
            con.execute(
                "INSERT OR REPLACE INTO auth_oauth (provider, provider_uid, username) VALUES (?,?,?)",
                (provider, provider_uid, username)
            )
            con.commit()
        finally:
            con.close()

    # --- Authentication ---

    async def auth_password(self, username: str, password: str) -> Optional[str]:
        """Verify password. Returns a session token on success, None on failure."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._auth_password_sync, username, password)

    def _auth_password_sync(self, username: str, password: str) -> Optional[str]:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT salt, hash FROM auth_password WHERE username=?", (username,)
            ).fetchone()
            if not row:
                return None
            if _hash_password(password, row["salt"]) != row["hash"]:
                return None
            return self._issue_session_sync(con, username)
        finally:
            con.close()

    async def auth_oauth(self, provider: str, provider_uid: str) -> Optional[str]:
        """Look up OAuth identity and return a session token if found."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._auth_oauth_sync, provider, provider_uid)

    def _auth_oauth_sync(self, provider: str, provider_uid: str) -> Optional[str]:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT username FROM auth_oauth WHERE provider=? AND provider_uid=?",
                (provider, provider_uid)
            ).fetchone()
            if not row:
                return None
            return self._issue_session_sync(con, row["username"])
        finally:
            con.close()

    async def register_or_login_oauth(
        self,
        provider: str,
        provider_uid: str,
        suggested_username: str,
        display_name: Optional[str] = None,
    ) -> tuple[str, str]:
        """
        Find existing OAuth account or create a new one.
        Returns (session_token, username).
        """
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(
            None, self._register_or_login_oauth_sync,
            provider, provider_uid, suggested_username, display_name
        )

    def _register_or_login_oauth_sync(
        self, provider: str, provider_uid: str, suggested_username: str, display_name: Optional[str]
    ) -> tuple[str, str]:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT username FROM auth_oauth WHERE provider=? AND provider_uid=?",
                (provider, provider_uid)
            ).fetchone()
            if row:
                token = self._issue_session_sync(con, row["username"])
                return token, row["username"]

            # Auto-resolve username collisions
            username = suggested_username
            suffix = 1
            while con.execute("SELECT 1 FROM accounts WHERE username=?", (username,)).fetchone():
                username = f"{suggested_username}{suffix}"
                suffix += 1

            priv, pub = generate_keypair()
            pub_bytes = public_key_to_bytes(pub)
            address = pub_to_address(pub_bytes)
            pub_b64 = b64e(pub_bytes)
            priv_b64 = b64e(private_key_to_bytes(priv))

            con.execute(
                "INSERT INTO accounts (username, display_name, flux_address, flux_pub_b64, flux_priv_b64, created_at) VALUES (?,?,?,?,?,?)",
                (username, display_name, address, pub_b64, priv_b64, _now())
            )
            con.execute(
                "INSERT INTO auth_oauth (provider, provider_uid, username) VALUES (?,?,?)",
                (provider, provider_uid, username)
            )
            con.commit()
            token = self._issue_session_sync(con, username)
            return token, username
        finally:
            con.close()

    def _issue_session_sync(self, con: sqlite3.Connection, username: str, ttl: int = 86400 * 30) -> str:
        """Issue a session token valid for `ttl` seconds (default 30 days)."""
        token = secrets.token_urlsafe(32)
        now = _now()
        # Clean up old sessions for this user first
        con.execute("DELETE FROM sessions WHERE username=? AND expires_at < ?", (username, now))
        con.execute(
            "INSERT INTO sessions (token, username, created_at, expires_at) VALUES (?,?,?,?)",
            (token, username, now, now + ttl)
        )
        con.commit()
        return token

    async def validate_session(self, token: str) -> Optional[str]:
        """Return the username for a valid session token, or None."""
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._validate_session_sync, token)

    def _validate_session_sync(self, token: str) -> Optional[str]:
        con = self._connect()
        try:
            row = con.execute(
                "SELECT username, expires_at FROM sessions WHERE token=?", (token,)
            ).fetchone()
            if not row or row["expires_at"] < _now():
                return None
            return row["username"]
        finally:
            con.close()

    async def revoke_session(self, token: str):
        loop = asyncio.get_event_loop()
        await loop.run_in_executor(None, self._revoke_sync, token)

    def _revoke_sync(self, token: str):
        con = self._connect()
        try:
            con.execute("DELETE FROM sessions WHERE token=?", (token,))
            con.commit()
        finally:
            con.close()

    # --- Lookups ---

    async def get_by_username(self, username: str) -> Optional[dict]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._get_by_username_sync, username)

    def _get_by_username_sync(self, username: str) -> Optional[dict]:
        con = self._connect()
        try:
            row = con.execute("SELECT * FROM accounts WHERE username=?", (username,)).fetchone()
            return dict(row) if row else None
        finally:
            con.close()

    async def get_identity(self, username: str) -> Optional[FluxIdentity]:
        """Load the FLUX identity (keypair) for a local account."""
        account = await self.get_by_username(username)
        if not account:
            return None
        from .crypto import b64d, private_key_from_bytes
        priv = private_key_from_bytes(b64d(account["flux_priv_b64"]))
        return FluxIdentity(priv, priv.public_key())

    async def get_public_profile(self, username: str) -> Optional[dict]:
        """Public-facing profile — what a remote server is allowed to see."""
        account = await self.get_by_username(username)
        if not account:
            return None
        return {
            "username": account["username"],
            "display_name": account["display_name"],
            "flux_address": account["flux_address"],
            "flux_pub_b64": account["flux_pub_b64"],
        }

    async def list_usernames(self) -> list[str]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._list_sync)

    def _list_sync(self) -> list[str]:
        con = self._connect()
        try:
            return [r[0] for r in con.execute("SELECT username FROM accounts ORDER BY username").fetchall()]
        finally:
            con.close()
