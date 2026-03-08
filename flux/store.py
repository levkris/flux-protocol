import asyncio
import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from collections import defaultdict
from pathlib import Path

from .constants import MAX_PENDING_PER_ADDRESS

log = logging.getLogger("flux.store")


class BaseStore(ABC):
    """Interface all store backends must implement."""

    @abstractmethod
    async def enqueue(self, msg: dict) -> bool: ...

    @abstractmethod
    async def drain(self, address: str) -> list[dict]: ...

    @abstractmethod
    async def peek_count(self, address: str) -> int: ...

    @abstractmethod
    async def ack(self, msg_id: str) -> bool: ...

    @abstractmethod
    async def stats(self) -> dict: ...


class MemoryStore(BaseStore):
    """In-memory store. Fast, zero dependencies. Does not survive restarts."""

    def __init__(self):
        self._pending: dict[str, list] = defaultdict(list)
        self._delivered: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, msg: dict) -> bool:
        async with self._lock:
            addr = msg["to"]
            if len(self._pending[addr]) >= MAX_PENDING_PER_ADDRESS:
                return False
            self._pending[addr].append(msg)
            return True

    async def drain(self, address: str) -> list[dict]:
        async with self._lock:
            msgs = self._pending.pop(address, [])
            for m in msgs:
                self._delivered[m["id"]] = m
            return msgs

    async def peek_count(self, address: str) -> int:
        async with self._lock:
            return len(self._pending.get(address, []))

    async def ack(self, msg_id: str) -> bool:
        async with self._lock:
            return self._delivered.pop(msg_id, None) is not None

    async def stats(self) -> dict:
        async with self._lock:
            queued = sum(len(v) for v in self._pending.values())
            return {
                "backend": "memory",
                "queued": queued,
                "addresses": len(self._pending),
                "delivered_unacked": len(self._delivered),
            }


class SQLiteStore(BaseStore):
    """
    Persistent SQLite store. Messages survive server restarts.
    Uses a thread-pool executor so the sync sqlite3 calls don't block the event loop.
    """

    def __init__(self, db_path: str | Path = "flux.db"):
        self._path = str(db_path)
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self):
        con = sqlite3.connect(self._path)
        con.execute("""
            CREATE TABLE IF NOT EXISTS messages (
                id TEXT PRIMARY KEY,
                address TEXT NOT NULL,
                payload TEXT NOT NULL,
                status TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL
            )
        """)
        con.execute("CREATE INDEX IF NOT EXISTS idx_address_status ON messages(address, status)")
        con.commit()
        con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path)
        con.row_factory = sqlite3.Row
        return con

    async def enqueue(self, msg: dict) -> bool:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._enqueue_sync, msg)

    def _enqueue_sync(self, msg: dict) -> bool:
        con = self._connect()
        try:
            count = con.execute(
                "SELECT COUNT(*) FROM messages WHERE address=? AND status='pending'",
                (msg["to"],)
            ).fetchone()[0]
            if count >= MAX_PENDING_PER_ADDRESS:
                return False
            con.execute(
                "INSERT OR IGNORE INTO messages (id, address, payload, status, created_at) VALUES (?,?,?,?,?)",
                (msg["id"], msg["to"], json.dumps(msg), "pending", msg["t"])
            )
            con.commit()
            return True
        finally:
            con.close()

    async def drain(self, address: str) -> list[dict]:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._drain_sync, address)

    def _drain_sync(self, address: str) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, payload FROM messages WHERE address=? AND status='pending' ORDER BY created_at",
                (address,)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                con.execute(
                    f"UPDATE messages SET status='delivered' WHERE id IN ({placeholders})", ids
                )
                con.commit()
            return [json.loads(r["payload"]) for r in rows]
        finally:
            con.close()

    async def peek_count(self, address: str) -> int:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._peek_sync, address)

    def _peek_sync(self, address: str) -> int:
        con = self._connect()
        try:
            return con.execute(
                "SELECT COUNT(*) FROM messages WHERE address=? AND status='pending'", (address,)
            ).fetchone()[0]
        finally:
            con.close()

    async def ack(self, msg_id: str) -> bool:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._ack_sync, msg_id)

    def _ack_sync(self, msg_id: str) -> bool:
        con = self._connect()
        try:
            cur = con.execute(
                "DELETE FROM messages WHERE id=? AND status='delivered'", (msg_id,)
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    async def stats(self) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._stats_sync)

    def _stats_sync(self) -> dict:
        con = self._connect()
        try:
            total = con.execute("SELECT COUNT(*) FROM messages").fetchone()[0]
            pending = con.execute("SELECT COUNT(*) FROM messages WHERE status='pending'").fetchone()[0]
            delivered = con.execute("SELECT COUNT(*) FROM messages WHERE status='delivered'").fetchone()[0]
            addresses = con.execute(
                "SELECT COUNT(DISTINCT address) FROM messages WHERE status='pending'"
            ).fetchone()[0]
            return {
                "backend": "sqlite",
                "total": total,
                "queued": pending,
                "delivered_unacked": delivered,
                "addresses": addresses,
            }
        finally:
            con.close()


def create_store(backend: str = "memory", **kwargs) -> BaseStore:
    """Factory — swap backends without touching server code."""
    if backend == "sqlite":
        return SQLiteStore(**kwargs)
    return MemoryStore()
