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
    async def list_messages(self, address: str, status: str | None = None) -> list[dict]: ...

    @abstractmethod
    async def mark_read(self, msg_id: str, address: str) -> bool: ...

    @abstractmethod
    async def delete_message(self, msg_id: str, address: str) -> bool: ...

    @abstractmethod
    async def stats(self) -> dict: ...


class MemoryStore(BaseStore):
    """In-memory store. Fast, zero dependencies. Does not survive restarts."""

    def __init__(self):
        self._messages: dict[str, dict] = {}   # msg_id → {msg, status, address}
        self._lock = asyncio.Lock()

    async def enqueue(self, msg: dict) -> bool:
        async with self._lock:
            addr = msg["to"]
            pending = sum(
                1 for m in self._messages.values()
                if m["address"] == addr and m["status"] == "pending"
            )
            if pending >= MAX_PENDING_PER_ADDRESS:
                return False
            self._messages[msg["id"]] = {
                "msg": msg,
                "address": addr,
                "status": "pending",
            }
            return True

    async def drain(self, address: str) -> list[dict]:
        """Mark all pending messages as delivered and return them."""
        async with self._lock:
            msgs = []
            for entry in self._messages.values():
                if entry["address"] == address and entry["status"] == "pending":
                    entry["status"] = "delivered"
                    msgs.append(entry["msg"])
            return sorted(msgs, key=lambda m: m["t"])

    async def peek_count(self, address: str) -> int:
        async with self._lock:
            return sum(
                1 for m in self._messages.values()
                if m["address"] == address and m["status"] == "pending"
            )

    async def list_messages(self, address: str, status: str | None = None) -> list[dict]:
        """Return all messages for an address, optionally filtered by status."""
        async with self._lock:
            results = []
            for entry in self._messages.values():
                if entry["address"] == address:
                    if status is None or entry["status"] == status:
                        results.append({**entry["msg"], "_status": entry["status"]})
            return sorted(results, key=lambda m: m["t"])

    async def mark_read(self, msg_id: str, address: str) -> bool:
        """Mark a delivered message as read. Returns False if not found or not owned."""
        async with self._lock:
            entry = self._messages.get(msg_id)
            if not entry or entry["address"] != address:
                return False
            if entry["status"] not in ("delivered", "pending"):
                return False
            entry["status"] = "read"
            return True

    async def delete_message(self, msg_id: str, address: str) -> bool:
        """
        Soft-delete a message (mark as deleted). Only the recipient can delete.
        Messages are never physically removed — they stay in the store as 'deleted'.
        """
        async with self._lock:
            entry = self._messages.get(msg_id)
            if not entry or entry["address"] != address:
                return False
            entry["status"] = "deleted"
            return True

    async def stats(self) -> dict:
        async with self._lock:
            by_status: dict[str, int] = defaultdict(int)
            for entry in self._messages.values():
                by_status[entry["status"]] += 1
            return {
                "backend": "memory",
                "total": len(self._messages),
                "pending": by_status.get("pending", 0),
                "delivered": by_status.get("delivered", 0),
                "read": by_status.get("read", 0),
                "deleted": by_status.get("deleted", 0),
                # legacy key kept for compatibility
                "queued": by_status.get("pending", 0),
                "addresses": len({e["address"] for e in self._messages.values()}),
                "delivered_unacked": by_status.get("delivered", 0),
            }


class SQLiteStore(BaseStore):
    """
    Persistent SQLite store. Messages survive server restarts.
    Uses a thread-pool executor so the sync sqlite3 calls don't block the event loop.

    Status lifecycle:
        pending   — received, not yet fetched by recipient
        delivered — fetched/pushed to recipient, not yet read
        read      — recipient has explicitly marked as read
        deleted   — soft-deleted by recipient; never physically removed
    """

    def __init__(self, db_path: str | Path = "flux.db"):
        self._path = str(db_path)
        self._lock = asyncio.Lock()
        self._init_db()

    def _init_db(self):
        con = sqlite3.connect(self._path)
        con.executescript("""
            CREATE TABLE IF NOT EXISTS messages (
                id         TEXT PRIMARY KEY,
                address    TEXT NOT NULL,
                payload    TEXT NOT NULL,
                status     TEXT NOT NULL DEFAULT 'pending',
                created_at INTEGER NOT NULL
            );

            CREATE INDEX IF NOT EXISTS idx_address_status ON messages(address, status);
            CREATE INDEX IF NOT EXISTS idx_address_created ON messages(address, created_at);
        """)
        con.commit()
        con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        return con

    # --- enqueue ---

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

    # --- drain (marks pending → delivered, returns messages) ---

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

    # --- peek ---

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

    # --- list all messages (inbox view) ---

    async def list_messages(self, address: str, status: str | None = None) -> list[dict]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._list_sync, address, status)

    def _list_sync(self, address: str, status: str | None) -> list[dict]:
        con = self._connect()
        try:
            if status:
                rows = con.execute(
                    "SELECT payload, status FROM messages WHERE address=? AND status=? ORDER BY created_at",
                    (address, status)
                ).fetchall()
            else:
                # Exclude deleted messages from default inbox view
                rows = con.execute(
                    "SELECT payload, status FROM messages WHERE address=? AND status != 'deleted' ORDER BY created_at",
                    (address,)
                ).fetchall()
            result = []
            for r in rows:
                msg = json.loads(r["payload"])
                msg["_status"] = r["status"]
                result.append(msg)
            return result
        finally:
            con.close()

    # --- mark read ---

    async def mark_read(self, msg_id: str, address: str) -> bool:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._mark_read_sync, msg_id, address)

    def _mark_read_sync(self, msg_id: str, address: str) -> bool:
        con = self._connect()
        try:
            cur = con.execute(
                "UPDATE messages SET status='read' WHERE id=? AND address=? AND status IN ('pending','delivered')",
                (msg_id, address)
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    # --- soft delete (owner only, never physically removed) ---

    async def delete_message(self, msg_id: str, address: str) -> bool:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._delete_sync, msg_id, address)

    def _delete_sync(self, msg_id: str, address: str) -> bool:
        con = self._connect()
        try:
            cur = con.execute(
                "UPDATE messages SET status='deleted' WHERE id=? AND address=? AND status != 'deleted'",
                (msg_id, address)
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    # --- stats ---

    async def stats(self) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._stats_sync)

    def _stats_sync(self) -> dict:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT status, COUNT(*) as cnt FROM messages GROUP BY status"
            ).fetchall()
            by_status = {r["status"]: r["cnt"] for r in rows}
            total = sum(by_status.values())
            addresses = con.execute(
                "SELECT COUNT(DISTINCT address) FROM messages WHERE status != 'deleted'"
            ).fetchone()[0]
            return {
                "backend": "sqlite",
                "total": total,
                "pending": by_status.get("pending", 0),
                "delivered": by_status.get("delivered", 0),
                "read": by_status.get("read", 0),
                "deleted": by_status.get("deleted", 0),
                # legacy keys kept for compatibility
                "queued": by_status.get("pending", 0),
                "delivered_unacked": by_status.get("delivered", 0),
                "addresses": addresses,
            }
        finally:
            con.close()


def create_store(backend: str = "memory", **kwargs) -> BaseStore:
    """Factory — swap backends without touching server code."""
    if backend == "sqlite":
        return SQLiteStore(**kwargs)
    return MemoryStore()