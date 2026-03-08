import asyncio
import json
import logging
import sqlite3
from abc import ABC, abstractmethod
from collections import defaultdict
from pathlib import Path
from typing import Optional

from .constants import MAX_PENDING_PER_ADDRESS

log = logging.getLogger("flux.store")

DEFAULT_INBOX = "inbox"


class BaseStore(ABC):

    @abstractmethod
    async def enqueue(self, msg: dict, inbox: str = DEFAULT_INBOX) -> bool: ...

    @abstractmethod
    async def drain(self, address: str, inbox: str = DEFAULT_INBOX) -> list[dict]: ...

    @abstractmethod
    async def peek_count(self, address: str, inbox: str = DEFAULT_INBOX) -> int: ...

    @abstractmethod
    async def list_messages(
        self,
        address: str,
        inbox: str = DEFAULT_INBOX,
        status: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict]: ...

    @abstractmethod
    async def mark_read(self, msg_id: str, address: str) -> bool: ...

    @abstractmethod
    async def delete_message(self, msg_id: str, address: str) -> bool: ...

    @abstractmethod
    async def add_tag(self, msg_id: str, address: str, tag: str) -> bool: ...

    @abstractmethod
    async def remove_tag(self, msg_id: str, address: str, tag: str) -> bool: ...

    @abstractmethod
    async def move_inbox(self, msg_id: str, address: str, inbox: str) -> bool: ...

    @abstractmethod
    async def list_inboxes(self, address: str) -> list[str]: ...

    @abstractmethod
    async def stats(self) -> dict: ...


class MemoryStore(BaseStore):

    def __init__(self):
        self._messages: dict[str, dict] = {}
        self._lock = asyncio.Lock()

    async def enqueue(self, msg: dict, inbox: str = DEFAULT_INBOX) -> bool:
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
                "inbox": inbox,
                "tags": set(t.lower() for t in (msg.get("tags") or [])),
            }
            return True

    async def drain(self, address: str, inbox: str = DEFAULT_INBOX) -> list[dict]:
        async with self._lock:
            msgs = []
            for entry in self._messages.values():
                if entry["address"] == address and entry["inbox"] == inbox and entry["status"] == "pending":
                    entry["status"] = "delivered"
                    msgs.append(self._export(entry))
            return sorted(msgs, key=lambda m: m["t"])

    async def peek_count(self, address: str, inbox: str = DEFAULT_INBOX) -> int:
        async with self._lock:
            return sum(
                1 for m in self._messages.values()
                if m["address"] == address and m["inbox"] == inbox and m["status"] == "pending"
            )

    async def list_messages(
        self,
        address: str,
        inbox: str = DEFAULT_INBOX,
        status: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict]:
        async with self._lock:
            results = []
            for entry in self._messages.values():
                if entry["address"] != address or entry["inbox"] != inbox:
                    continue
                if status:
                    if entry["status"] != status:
                        continue
                elif entry["status"] == "deleted":
                    continue
                if tag and tag.lower() not in entry["tags"]:
                    continue
                results.append(self._export(entry))
            return sorted(results, key=lambda m: m["t"])

    async def mark_read(self, msg_id: str, address: str) -> bool:
        from .message import is_expired
        async with self._lock:
            entry = self._messages.get(msg_id)
            if not entry or entry["address"] != address:
                return False
            if entry["status"] not in ("pending", "delivered"):
                return False
            if is_expired(entry["msg"]) or entry["msg"].get("expires") is not None:
                entry["status"] = "deleted"
            else:
                entry["status"] = "read"
            return True

    async def delete_message(self, msg_id: str, address: str) -> bool:
        async with self._lock:
            entry = self._messages.get(msg_id)
            if not entry or entry["address"] != address or entry["status"] == "deleted":
                return False
            entry["status"] = "deleted"
            return True

    async def add_tag(self, msg_id: str, address: str, tag: str) -> bool:
        async with self._lock:
            entry = self._messages.get(msg_id)
            if not entry or entry["address"] != address:
                return False
            entry["tags"].add(tag.lower())
            return True

    async def remove_tag(self, msg_id: str, address: str, tag: str) -> bool:
        async with self._lock:
            entry = self._messages.get(msg_id)
            if not entry or entry["address"] != address:
                return False
            entry["tags"].discard(tag.lower())
            return True

    async def move_inbox(self, msg_id: str, address: str, inbox: str) -> bool:
        async with self._lock:
            entry = self._messages.get(msg_id)
            if not entry or entry["address"] != address:
                return False
            entry["inbox"] = inbox
            return True

    async def list_inboxes(self, address: str) -> list[str]:
        async with self._lock:
            return sorted({e["inbox"] for e in self._messages.values() if e["address"] == address})

    def _export(self, entry: dict) -> dict:
        m = dict(entry["msg"])
        m["_status"] = entry["status"]
        m["_inbox"] = entry["inbox"]
        m["_tags"] = sorted(entry["tags"])
        return m

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
                "queued": by_status.get("pending", 0),
                "addresses": len({e["address"] for e in self._messages.values()}),
                "delivered_unacked": by_status.get("delivered", 0),
            }


class SQLiteStore(BaseStore):

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
                inbox      TEXT NOT NULL DEFAULT 'inbox',
                created_at INTEGER NOT NULL
            );

            CREATE TABLE IF NOT EXISTS message_tags (
                msg_id  TEXT NOT NULL REFERENCES messages(id),
                tag     TEXT NOT NULL,
                PRIMARY KEY (msg_id, tag)
            );

            CREATE INDEX IF NOT EXISTS idx_address_status  ON messages(address, status);
            CREATE INDEX IF NOT EXISTS idx_address_inbox   ON messages(address, inbox);
            CREATE INDEX IF NOT EXISTS idx_address_created ON messages(address, created_at);
            CREATE INDEX IF NOT EXISTS idx_tags_tag        ON message_tags(tag);
        """)
        con.commit()
        con.close()

    def _connect(self) -> sqlite3.Connection:
        con = sqlite3.connect(self._path)
        con.row_factory = sqlite3.Row
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA foreign_keys=ON")
        return con

    async def enqueue(self, msg: dict, inbox: str = DEFAULT_INBOX) -> bool:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._enqueue_sync, msg, inbox)

    def _enqueue_sync(self, msg: dict, inbox: str) -> bool:
        con = self._connect()
        try:
            count = con.execute(
                "SELECT COUNT(*) FROM messages WHERE address=? AND status='pending'", (msg["to"],)
            ).fetchone()[0]
            if count >= MAX_PENDING_PER_ADDRESS:
                return False
            con.execute(
                "INSERT OR IGNORE INTO messages (id, address, payload, status, inbox, created_at) VALUES (?,?,?,?,?,?)",
                (msg["id"], msg["to"], json.dumps(msg), "pending", inbox, msg["t"])
            )
            for tag in (msg.get("tags") or []):
                con.execute(
                    "INSERT OR IGNORE INTO message_tags (msg_id, tag) VALUES (?,?)",
                    (msg["id"], tag.lower())
                )
            con.commit()
            return True
        finally:
            con.close()

    async def drain(self, address: str, inbox: str = DEFAULT_INBOX) -> list[dict]:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._drain_sync, address, inbox)

    def _drain_sync(self, address: str, inbox: str) -> list[dict]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT id, payload FROM messages WHERE address=? AND inbox=? AND status='pending' ORDER BY created_at",
                (address, inbox)
            ).fetchall()
            ids = [r["id"] for r in rows]
            if ids:
                placeholders = ",".join("?" * len(ids))
                con.execute(f"UPDATE messages SET status='delivered' WHERE id IN ({placeholders})", ids)
                con.commit()
            return [self._hydrate(con, r["id"], json.loads(r["payload"]), "delivered", inbox) for r in rows]
        finally:
            con.close()

    async def peek_count(self, address: str, inbox: str = DEFAULT_INBOX) -> int:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._peek_sync, address, inbox)

    def _peek_sync(self, address: str, inbox: str) -> int:
        con = self._connect()
        try:
            return con.execute(
                "SELECT COUNT(*) FROM messages WHERE address=? AND inbox=? AND status='pending'",
                (address, inbox)
            ).fetchone()[0]
        finally:
            con.close()

    async def list_messages(
        self,
        address: str,
        inbox: str = DEFAULT_INBOX,
        status: Optional[str] = None,
        tag: Optional[str] = None,
    ) -> list[dict]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._list_sync, address, inbox, status, tag)

    def _list_sync(self, address: str, inbox: str, status: Optional[str], tag: Optional[str]) -> list[dict]:
        con = self._connect()
        try:
            if tag:
                q = """
                    SELECT m.id, m.payload, m.status, m.inbox FROM messages m
                    JOIN message_tags t ON t.msg_id = m.id
                    WHERE m.address=? AND m.inbox=? AND t.tag=?
                """
                args = [address, inbox, tag.lower()]
                if status:
                    q += " AND m.status=?"
                    args.append(status)
                else:
                    q += " AND m.status != 'deleted'"
                rows = con.execute(q + " ORDER BY m.created_at", args).fetchall()
            elif status:
                rows = con.execute(
                    "SELECT id, payload, status, inbox FROM messages WHERE address=? AND inbox=? AND status=? ORDER BY created_at",
                    (address, inbox, status)
                ).fetchall()
            else:
                rows = con.execute(
                    "SELECT id, payload, status, inbox FROM messages WHERE address=? AND inbox=? AND status != 'deleted' ORDER BY created_at",
                    (address, inbox)
                ).fetchall()
            return [self._hydrate(con, r["id"], json.loads(r["payload"]), r["status"], r["inbox"]) for r in rows]
        finally:
            con.close()

    def _hydrate(self, con, msg_id: str, payload: dict, status: str, inbox: str) -> dict:
        tags = [r[0] for r in con.execute(
            "SELECT tag FROM message_tags WHERE msg_id=? ORDER BY tag", (msg_id,)
        ).fetchall()]
        m = dict(payload)
        m["_status"] = status
        m["_inbox"] = inbox
        m["_tags"] = tags
        return m

    async def mark_read(self, msg_id: str, address: str) -> bool:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._mark_read_sync, msg_id, address)

    def _mark_read_sync(self, msg_id: str, address: str) -> bool:
        from .message import is_expired
        con = self._connect()
        try:
            row = con.execute(
                "SELECT payload, status FROM messages WHERE id=? AND address=?", (msg_id, address)
            ).fetchone()
            if not row or row["status"] not in ("pending", "delivered"):
                return False
            msg = json.loads(row["payload"])
            new_status = "deleted" if (is_expired(msg) or msg.get("expires") is not None) else "read"
            cur = con.execute(
                "UPDATE messages SET status=? WHERE id=? AND address=?", (new_status, msg_id, address)
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

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

    async def add_tag(self, msg_id: str, address: str, tag: str) -> bool:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._add_tag_sync, msg_id, address, tag)

    def _add_tag_sync(self, msg_id: str, address: str, tag: str) -> bool:
        con = self._connect()
        try:
            if not con.execute("SELECT 1 FROM messages WHERE id=? AND address=?", (msg_id, address)).fetchone():
                return False
            con.execute("INSERT OR IGNORE INTO message_tags (msg_id, tag) VALUES (?,?)", (msg_id, tag.lower()))
            con.commit()
            return True
        finally:
            con.close()

    async def remove_tag(self, msg_id: str, address: str, tag: str) -> bool:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._remove_tag_sync, msg_id, address, tag)

    def _remove_tag_sync(self, msg_id: str, address: str, tag: str) -> bool:
        con = self._connect()
        try:
            if not con.execute("SELECT 1 FROM messages WHERE id=? AND address=?", (msg_id, address)).fetchone():
                return False
            cur = con.execute("DELETE FROM message_tags WHERE msg_id=? AND tag=?", (msg_id, tag.lower()))
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    async def move_inbox(self, msg_id: str, address: str, inbox: str) -> bool:
        async with self._lock:
            loop = asyncio.get_event_loop()
            return await loop.run_in_executor(None, self._move_inbox_sync, msg_id, address, inbox)

    def _move_inbox_sync(self, msg_id: str, address: str, inbox: str) -> bool:
        con = self._connect()
        try:
            cur = con.execute(
                "UPDATE messages SET inbox=? WHERE id=? AND address=?", (inbox, msg_id, address)
            )
            con.commit()
            return cur.rowcount > 0
        finally:
            con.close()

    async def list_inboxes(self, address: str) -> list[str]:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._list_inboxes_sync, address)

    def _list_inboxes_sync(self, address: str) -> list[str]:
        con = self._connect()
        try:
            rows = con.execute(
                "SELECT DISTINCT inbox FROM messages WHERE address=? ORDER BY inbox", (address,)
            ).fetchall()
            return [r[0] for r in rows]
        finally:
            con.close()

    async def stats(self) -> dict:
        loop = asyncio.get_event_loop()
        return await loop.run_in_executor(None, self._stats_sync)

    def _stats_sync(self) -> dict:
        con = self._connect()
        try:
            rows = con.execute("SELECT status, COUNT(*) as cnt FROM messages GROUP BY status").fetchall()
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
                "queued": by_status.get("pending", 0),
                "delivered_unacked": by_status.get("delivered", 0),
                "addresses": addresses,
            }
        finally:
            con.close()


def create_store(backend: str = "memory", **kwargs) -> BaseStore:
    if backend == "sqlite":
        return SQLiteStore(**kwargs)
    return MemoryStore()