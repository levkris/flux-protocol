import logging

from aiohttp import web

from .auth import validate_token
from .constants import FLUX_VERSION, FLUX_API_VERSION
from .message import validate_fields, verify_message, check_freshness, append_route, strip_bcc
from .constants import DEFAULT_INBOX
from .integrity import (
    append_integrity_hop, verify_integrity_chain,
    record_tamper, is_quarantined, get_reputation,
    build_tamper_report, verify_tamper_report,
)
from .spam import is_spam

log = logging.getLogger("flux.routes")


def ok(data: dict | None = None) -> web.Response:
    body = {"ok": True}
    if data:
        body.update(data)
    return web.json_response(body)


def err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


async def _broadcast_tamper_report(report: dict, peers: list[str], offender: str):
    import aiohttp as _aiohttp
    async with _aiohttp.ClientSession() as session:
        for peer in peers:
            if offender in peer:
                continue
            try:
                async with session.post(
                    f"{peer}/integrity/tamper_report",
                    json=report,
                    timeout=_aiohttp.ClientTimeout(total=5),
                ) as r:
                    log.info(f"tamper report sent to {peer}: HTTP {r.status}")
            except Exception as e:
                log.warning(f"tamper report to {peer} failed: {e}")


def make_routes(store, presence, domain: str = "", mesh_relay=None):

    def _known_peers() -> list[str]:
        if not mesh_relay:
            return []
        peers = []
        for cfg in mesh_relay._meshes.values():
            peers.extend(cfg.get("peers", []))
        return list(set(peers))

    async def route_send(request: web.Request) -> web.Response:
        try:
            msg = await request.json()
        except Exception:
            return err("invalid json")

        if not validate_fields(msg):
            return err("missing required fields")
        if msg.get("v") != FLUX_VERSION:
            return err("unsupported protocol version")
        if not check_freshness(msg):
            return err("message too old or clock skew exceeds tolerance")
        if not verify_message(msg):
            return err("invalid signature", 403)

        spam_result = is_spam(msg)

        chain_ok, offender = verify_integrity_chain(msg)
        if not chain_ok and offender:
            record_tamper(offender)
            report = build_tamper_report(msg, offender, domain or request.host)
            peers = _known_peers()
            if peers:
                import asyncio
                asyncio.create_task(_broadcast_tamper_report(report, peers, offender))
            log.warning(f"tampered message from {offender} — rejected")
            return err("message integrity chain violated", 403)

        for hop in (msg.get("integrity_chain") or []):
            if is_quarantined(hop.get("server", "")):
                log.warning(f"rejecting message relayed by quarantined server {hop['server']}")
                return err("message relayed by untrusted server", 403)

        hop = domain or request.host
        msg = append_route(msg, hop)
        msg = append_integrity_hop(msg, hop)
        msg_clean = strip_bcc(msg)

        inbox = "spam" if spam_result["spam"] else DEFAULT_INBOX
        if spam_result["spam"]:
            log.info(f"spam from {msg.get('from','?')} routed to spam inbox: {spam_result['reason']}")

        # Spam is never pushed in realtime — always stored in the spam inbox
        if not spam_result["spam"] and await presence.deliver(msg_clean["to"], {"type": "msg", "msg": msg_clean}):
            delivery = "realtime"
        else:
            queued = await store.enqueue(msg_clean, inbox=inbox)
            if not queued:
                return err("mailbox full", 429)
            delivery = "queued"

        mesh_results = {}
        if mesh_relay:
            mesh_results = await mesh_relay.relay(msg_clean)

        return ok({"delivery": delivery, "mesh": mesh_results})

    async def route_fetch(request: web.Request) -> web.Response:
        address = request.match_info["address"]
        token = request.headers.get("X-Flux-Token", "")
        if not validate_token(address, token):
            return err("unauthorized", 401)
        inbox = request.rel_url.query.get("inbox", DEFAULT_INBOX)
        msgs = await store.drain(address, inbox)
        return ok({"messages": msgs, "count": len(msgs)})

    async def route_peek(request: web.Request) -> web.Response:
        address = request.match_info["address"]
        token = request.headers.get("X-Flux-Token", "")
        if not validate_token(address, token):
            return err("unauthorized", 401)
        inbox = request.rel_url.query.get("inbox", DEFAULT_INBOX)
        count = await store.peek_count(address, inbox)
        return ok({"count": count})

    async def route_inbox(request: web.Request) -> web.Response:
        address = request.match_info["address"]
        token = request.headers.get("X-Flux-Token", "")
        if not validate_token(address, token):
            return err("unauthorized", 401)
        inbox = request.rel_url.query.get("inbox", DEFAULT_INBOX)
        status_filter = request.rel_url.query.get("status")
        tag_filter = request.rel_url.query.get("tag")
        await store.drain(address, inbox)
        msgs = await store.list_messages(address, inbox, status=status_filter, tag=tag_filter)
        return ok({"messages": msgs, "count": len(msgs)})

    async def route_list_inboxes(request: web.Request) -> web.Response:
        address = request.match_info["address"]
        token = request.headers.get("X-Flux-Token", "")
        if not validate_token(address, token):
            return err("unauthorized", 401)
        inboxes = await store.list_inboxes(address)
        return ok({"inboxes": inboxes})

    async def route_read(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            msg_id = body.get("id", "")
            address = body.get("address", "")
        except Exception:
            return err("invalid json")
        if not msg_id or not address:
            return err("missing 'id' or 'address'")
        token = request.headers.get("X-Flux-Token", "")
        if not validate_token(address, token):
            return err("unauthorized", 401)
        found = await store.mark_read(msg_id, address)
        return ok({"read": found})

    async def route_delete(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            msg_id = body.get("id", "")
            address = body.get("address", "")
        except Exception:
            return err("invalid json")
        if not msg_id or not address:
            return err("missing 'id' or 'address'")
        token = request.headers.get("X-Flux-Token", "")
        if not validate_token(address, token):
            return err("unauthorized", 401)
        found = await store.delete_message(msg_id, address)
        return ok({"deleted": found})

    async def route_tag(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            msg_id = body.get("id", "")
            address = body.get("address", "")
            tag = body.get("tag", "").strip().lower()
            action = body.get("action", "add")
        except Exception:
            return err("invalid json")
        if not msg_id or not address or not tag:
            return err("missing 'id', 'address', or 'tag'")
        token = request.headers.get("X-Flux-Token", "")
        if not validate_token(address, token):
            return err("unauthorized", 401)
        if action == "add":
            found = await store.add_tag(msg_id, address, tag)
        elif action == "remove":
            found = await store.remove_tag(msg_id, address, tag)
        else:
            return err("action must be 'add' or 'remove'")
        return ok({"tag": tag, "action": action, "ok": found})

    async def route_move(request: web.Request) -> web.Response:
        try:
            body = await request.json()
            msg_id = body.get("id", "")
            address = body.get("address", "")
            inbox = body.get("inbox", "").strip()
        except Exception:
            return err("invalid json")
        if not msg_id or not address or not inbox:
            return err("missing 'id', 'address', or 'inbox'")
        token = request.headers.get("X-Flux-Token", "")
        if not validate_token(address, token):
            return err("unauthorized", 401)
        found = await store.move_inbox(msg_id, address, inbox)
        return ok({"moved": found, "inbox": inbox})

    async def route_status(request: web.Request) -> web.Response:
        address = request.match_info["address"]
        online = await presence.is_online(address)
        return ok({"address": address, "online": online})

    async def route_stats(request: web.Request) -> web.Response:
        store_stats = await store.stats()
        return ok({**store_stats, "online_addresses": presence.online_count(), "ws_connections": presence.connection_count()})

    async def route_health(request: web.Request) -> web.Response:
        return ok({"protocol": FLUX_VERSION, "version": FLUX_API_VERSION})

    async def route_tamper_report(request: web.Request) -> web.Response:
        try:
            report = await request.json()
        except Exception:
            return err("invalid json")
        if not verify_tamper_report(report):
            return err("invalid tamper report signature", 403)
        offender = report.get("offender", "")
        if not offender:
            return err("missing offender")
        strikes = record_tamper(offender)
        log.warning(f"received tamper report from {report.get('reporter','?')}: {offender} now has {strikes} strike(s)")
        return ok({"offender": offender, "strikes": strikes, "quarantined": is_quarantined(offender)})

    async def route_reputation(request: web.Request) -> web.Response:
        return ok(get_reputation())

    async def route_verify_message(request: web.Request) -> web.Response:
        try:
            msg = await request.json()
        except Exception:
            return err("invalid json")
        sig_ok = verify_message(msg)
        chain_ok, offender = verify_integrity_chain(msg)
        return ok({
            "signature_valid": sig_ok,
            "integrity_chain_valid": chain_ok,
            "offending_server": offender,
            "hop_count": len(msg.get("integrity_chain") or []),
        })

    return [
        web.post("/send", route_send),
        web.get("/fetch/{address}", route_fetch),
        web.get("/peek/{address}", route_peek),
        web.get("/inbox/{address}", route_inbox),
        web.get("/inboxes/{address}", route_list_inboxes),
        web.post("/read", route_read),
        web.post("/delete", route_delete),
        web.post("/tag", route_tag),
        web.post("/move", route_move),
        web.get("/status/{address}", route_status),
        web.get("/stats", route_stats),
        web.get("/health", route_health),
        web.post("/integrity/tamper_report", route_tamper_report),
        web.get("/integrity/reputation", route_reputation),
        web.post("/integrity/verify", route_verify_message),
    ]