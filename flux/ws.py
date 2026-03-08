import json
import logging

import aiohttp
from aiohttp import web

from .auth import validate_token
from .constants import FLUX_VERSION, WS_PING_INTERVAL, DEFAULT_INBOX
from .message import validate_fields, verify_message, check_freshness, now_ms, append_route, strip_bcc
from .integrity import (
    append_integrity_hop, verify_integrity_chain,
    record_tamper, is_quarantined, build_tamper_report,
)
from .spam import is_spam

log = logging.getLogger("flux.ws")


def make_ws_handler(store, presence, domain: str = "", mesh_relay=None):

    def _known_peers() -> list[str]:
        if not mesh_relay:
            return []
        peers = []
        for cfg in mesh_relay._meshes.values():
            peers.extend(cfg.get("peers", []))
        return list(set(peers))

    async def _broadcast_tamper(report: dict, offender: str):
        import asyncio
        import aiohttp as _aio
        async with _aio.ClientSession() as session:
            for peer in _known_peers():
                if offender in peer:
                    continue
                try:
                    await session.post(f"{peer}/integrity/tamper_report", json=report,
                                       timeout=_aio.ClientTimeout(total=5))
                except Exception:
                    pass

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
                        inbox = frame.get("inbox", DEFAULT_INBOX)
                        await store.drain(address, inbox)
                        messages = await store.list_messages(address, inbox)
                        await ws.send_str(json.dumps({
                            "ok": True, "action": "authed", "address": address,
                            "inbox": inbox, "messages": messages,
                        }))
                        log.info(f"authed {address} ({len(messages)} messages in inbox)")

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

                        spam_result = is_spam(msg)

                        chain_ok, offender = verify_integrity_chain(msg)
                        if not chain_ok and offender:
                            record_tamper(offender)
                            import asyncio
                            asyncio.create_task(_broadcast_tamper(
                                build_tamper_report(msg, offender, domain or request.host), offender
                            ))
                            await ws.send_str(json.dumps({"ok": False, "error": "integrity chain violated"}))
                            continue

                        if any(is_quarantined(h.get("server", "")) for h in (msg.get("integrity_chain") or [])):
                            await ws.send_str(json.dumps({"ok": False, "error": "message from quarantined server"}))
                            continue

                        hop = domain or request.host
                        msg = append_integrity_hop(append_route(msg, hop), hop)
                        msg_clean = strip_bcc(msg)

                        inbox = "spam" if spam_result["spam"] else DEFAULT_INBOX

                        # Spam is never pushed in realtime — always stored in the spam inbox
                        if not spam_result["spam"] and await presence.deliver(msg_clean["to"], {"type": "msg", "msg": msg_clean}):
                            delivery = "realtime"
                            result = True
                        else:
                            result = await store.enqueue(msg_clean, inbox=inbox)
                            delivery = "queued" if result else "dropped"

                        mesh_results = {}
                        if mesh_relay:
                            mesh_results = await mesh_relay.relay(msg_clean)

                        await ws.send_str(json.dumps({
                            "ok": result, "id": msg_clean["id"],
                            "delivery": delivery, "mesh": mesh_results,
                        }))

                    elif action == "read":
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
                        if not authed:
                            await ws.send_str(json.dumps({"ok": False, "error": "not authenticated"}))
                            continue
                        msg_id = frame.get("id", "")
                        if not msg_id:
                            await ws.send_str(json.dumps({"ok": False, "error": "missing id"}))
                            continue
                        found = await store.delete_message(msg_id, address)
                        await ws.send_str(json.dumps({"ok": True, "deleted": found}))

                    elif action == "tag":
                        if not authed:
                            await ws.send_str(json.dumps({"ok": False, "error": "not authenticated"}))
                            continue
                        msg_id = frame.get("id", "")
                        tag = frame.get("tag", "").strip().lower()
                        tag_action = frame.get("tag_action", "add")
                        if not msg_id or not tag:
                            await ws.send_str(json.dumps({"ok": False, "error": "missing id or tag"}))
                            continue
                        if tag_action == "add":
                            found = await store.add_tag(msg_id, address, tag)
                        else:
                            found = await store.remove_tag(msg_id, address, tag)
                        await ws.send_str(json.dumps({"ok": True, "tag": tag, "tag_action": tag_action, "applied": found}))

                    elif action == "move":
                        if not authed:
                            await ws.send_str(json.dumps({"ok": False, "error": "not authenticated"}))
                            continue
                        msg_id = frame.get("id", "")
                        inbox = frame.get("inbox", "").strip()
                        if not msg_id or not inbox:
                            await ws.send_str(json.dumps({"ok": False, "error": "missing id or inbox"}))
                            continue
                        found = await store.move_inbox(msg_id, address, inbox)
                        await ws.send_str(json.dumps({"ok": True, "moved": found, "inbox": inbox}))

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