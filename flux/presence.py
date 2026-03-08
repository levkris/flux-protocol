import asyncio
import json
import logging
from collections import defaultdict

log = logging.getLogger("flux.presence")


class PresenceRegistry:
    """
    Tracks live WebSocket connections per address.
    One address can have multiple simultaneous connections (e.g. multiple devices).
    """

    def __init__(self):
        self._sockets: dict[str, set] = defaultdict(set)
        self._lock = asyncio.Lock()

    async def register(self, address: str, ws):
        async with self._lock:
            self._sockets[address].add(ws)
        log.debug(f"registered {address} ({len(self._sockets[address])} connections)")

    async def unregister(self, address: str, ws):
        async with self._lock:
            self._sockets[address].discard(ws)
            if not self._sockets[address]:
                del self._sockets[address]
        log.debug(f"unregistered {address}")

    async def is_online(self, address: str) -> bool:
        async with self._lock:
            return bool(self._sockets.get(address))

    async def deliver(self, address: str, payload: dict) -> bool:
        """Push a frame to all active sockets for an address. Returns True if at least one succeeded."""
        async with self._lock:
            sockets = list(self._sockets.get(address, []))

        if not sockets:
            return False

        raw = json.dumps(payload)
        dead = []
        delivered = False

        for ws in sockets:
            try:
                await ws.send_str(raw)
                delivered = True
            except Exception:
                dead.append(ws)

        for ws in dead:
            await self.unregister(address, ws)

        return delivered

    def online_count(self) -> int:
        return len(self._sockets)

    def connection_count(self) -> int:
        return sum(len(v) for v in self._sockets.values())
