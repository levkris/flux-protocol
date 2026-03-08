import json
import logging
from typing import Callable, Awaitable, Optional

import aiohttp

from .auth import derive_token
from .identity import FluxIdentity
from .message import build_message

log = logging.getLogger("flux.client")


class FluxClient:
    """
    Async FLUX client. Supports both HTTP (stateless) and WebSocket (real-time) transports.
    Use HTTP for simple send/fetch. Use WebSocket for persistent connections with push delivery.
    """

    def __init__(self, identity: FluxIdentity, server: str = "http://localhost:8765"):
        self.identity = identity
        self._server = server.rstrip("/")
        self._ws_url = self._server.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
        self._session: Optional[aiohttp.ClientSession] = None
        self._ws: Optional[aiohttp.ClientWebSocketResponse] = None
        self._on_message: Optional[Callable] = None

    @property
    def address(self) -> str:
        return self.identity.address

    @property
    def token(self) -> str:
        return derive_token(self.identity.address)

    # --- Session management ---

    async def _sess(self) -> aiohttp.ClientSession:
        if not self._session or self._session.closed:
            self._session = aiohttp.ClientSession()
        return self._session

    async def close(self):
        if self._ws and not self._ws.closed:
            await self._ws.close()
        if self._session and not self._session.closed:
            await self._session.close()

    # --- HTTP transport ---

    async def send(self, to: str, content: str, reply_to: Optional[str] = None) -> dict:
        """Send a message via HTTP POST. Returns the server response."""
        msg = build_message(self.identity, to, content, reply_to)
        sess = await self._sess()
        async with sess.post(f"{self._server}/send", json=msg) as r:
            return await r.json()

    async def fetch(self) -> list[dict]:
        """Fetch and drain all pending messages from the server."""
        sess = await self._sess()
        async with sess.get(
            f"{self._server}/fetch/{self.address}",
            headers={"X-Flux-Token": self.token},
        ) as r:
            data = await r.json()
            return data.get("messages", [])

    async def peek(self) -> int:
        """Check how many messages are waiting without draining them."""
        sess = await self._sess()
        async with sess.get(
            f"{self._server}/peek/{self.address}",
            headers={"X-Flux-Token": self.token},
        ) as r:
            data = await r.json()
            return data.get("count", 0)

    async def ack(self, msg_id: str) -> bool:
        """Acknowledge a delivered message so it is removed from the server."""
        sess = await self._sess()
        async with sess.post(f"{self._server}/ack", json={"id": msg_id}) as r:
            data = await r.json()
            return data.get("acked", False)

    async def status(self, address: str) -> bool:
        """Check whether an address is currently connected via WebSocket."""
        sess = await self._sess()
        async with sess.get(f"{self._server}/status/{address}") as r:
            data = await r.json()
            return data.get("online", False)

    # --- WebSocket transport ---

    def on_message(self, handler: Callable[..., Awaitable]):
        """Register an async callback invoked whenever a message arrives over WebSocket."""
        self._on_message = handler
        return handler

    async def connect_ws(self):
        """
        Open a persistent WebSocket connection and authenticate.
        Queued messages are flushed immediately on connect.
        Runs until the connection is closed — call this as a background task.
        """
        sess = await self._sess()
        self._ws = await sess.ws_connect(self._ws_url)

        await self._ws.send_str(json.dumps({
            "action": "auth",
            "address": self.address,
            "token": self.token,
        }))

        async for raw in self._ws:
            if raw.type == aiohttp.WSMsgType.TEXT:
                frame = json.loads(raw.data)

                if frame.get("action") == "authed":
                    for msg in frame.get("queued", []):
                        if self._on_message:
                            await self._on_message(msg)

                elif frame.get("type") == "msg":
                    if self._on_message:
                        await self._on_message(frame["msg"])

            elif raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                break

        self._ws = None

    async def send_ws(self, to: str, content: str, reply_to: Optional[str] = None) -> dict:
        """Send a message over an active WebSocket connection."""
        if not self._ws or self._ws.closed:
            raise RuntimeError("WebSocket not connected — call connect_ws() first")
        msg = build_message(self.identity, to, content, reply_to)
        await self._ws.send_str(json.dumps({"action": "send", "msg": msg}))
        return msg
