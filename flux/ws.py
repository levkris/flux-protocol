import json
import logging

import aiohttp
from aiohttp import web

from .auth import validate_token
from .constants import FLUX_VERSION, WS_PING_INTERVAL
from .message import validate_fields, verify_message, check_freshness, now_ms

log = logging.getLogger("flux.ws")


def make_ws_handler(store, presence):
    """Return a WebSocket handler bound to a store and presence registry."""

    async def ws_handler(request: web.Request) -> web.WebSocketResponse:
        ws = web.WebSocketResponse(heartbeat=WS_PING_INTERVAL)
        await ws.prepare(request)

        address = None
        authed = False

        try:
            async for raw in ws:
                if raw.type == aiohttp.WSMsgType.TEXT:
                    try:
                        frame = json.loads(raw.data)
                    except Exception:
                        await ws.send_str(json.dumps({"ok": False, "error": "bad json"}))
                        continue

                    action = frame.get("action")

                    if action == "auth":
                        addr = frame.get("address", "")
                        token = frame.get("token", "")

                        if not validate_token(addr, token):
                            await ws.send_str(json.dumps({"ok": False, "error": "unauthorized"}))
                            continue

                        address = addr
                        authed = True
                        await presence.register(address, ws)

                        # Drain pending messages on connect (marks them delivered)
                        # then return the full inbox so the client has everything
                        await store.drain(address)
                        inbox = await store.list_messages(address)
                        await ws.send_str(json.dumps({
                            "ok": True,
                            "action": "authed",
                            "address": address,
                            "messages": inbox,
                        }))
                        log.info(f"authed {address} ({len(inbox)} messages in inbox)")

                    elif action == "send":
                        if not authed:
                            await ws.send_str(json.dumps({"ok": False, "error": "not authenticated"}))
                            continue

                        msg = frame.get("msg", {})

                        if not validate_fields(msg):
                            await ws.send_str(json.dumps({"ok": False, "error": "missing fields"}))
                            continue

                        if msg.get("v") != FLUX_VERSION:
                            await ws.send_str(json.dumps({"ok": False, "error": "unsupported version"}))
                            continue

                        if not check_freshness(msg):
                            await ws.send_str(json.dumps({"ok": False, "error": "clock skew"}))
                            continue

                        if not verify_message(msg):
                            await ws.send_str(json.dumps({"ok": False, "error": "invalid signature"}))
                            continue

                        if await presence.deliver(msg["to"], {"type": "msg", "msg": msg}):
                            delivery = "realtime"
                            result = True
                        else:
                            result = await store.enqueue(msg)
                            delivery = "queued" if result else "dropped"

                        await ws.send_str(json.dumps({
                            "ok": result,
                            "id": msg["id"],
                            "delivery": delivery,
                        }))

                    elif action == "read":
                        # Mark a message as read - only the authenticated user's messages
                        if not authed:
                            await ws.send_str(json.dumps({"ok": False, "error": "not authenticated"}))
                            continue

                        msg_id = frame.get("id", "")
                        if not msg_id:
                            await ws.send_str(json.dumps({"ok": False, "error": "missing id"}))
                            continue

                        found = await store.mark_read(msg_id, address)
                        await ws.send_str(json.dumps({"ok": True, "read": found}))

                    elif action == "delete":
                        # Soft-delete a message - only the authenticated user's messages
                        if not authed:
                            await ws.send_str(json.dumps({"ok": False, "error": "not authenticated"}))
                            continue

                        msg_id = frame.get("id", "")
                        if not msg_id:
                            await ws.send_str(json.dumps({"ok": False, "error": "missing id"}))
                            continue

                        found = await store.delete_message(msg_id, address)
                        await ws.send_str(json.dumps({"ok": True, "deleted": found}))

                    elif action == "ping":
                        await ws.send_str(json.dumps({"ok": True, "action": "pong", "t": now_ms()}))

                    else:
                        await ws.send_str(json.dumps({"ok": False, "error": "unknown action"}))

                elif raw.type in (aiohttp.WSMsgType.ERROR, aiohttp.WSMsgType.CLOSE):
                    break

        finally:
            if address and authed:
                await presence.unregister(address, ws)
                log.info(f"disconnected {address}")

        return ws

    return ws_handler