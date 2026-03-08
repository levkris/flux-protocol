import logging

from aiohttp import web

from .auth import validate_token
from .constants import FLUX_VERSION
from .message import validate_fields, verify_message, check_freshness

log = logging.getLogger("flux.routes")


def ok(data: dict | None = None) -> web.Response:
    body = {"ok": True}
    if data:
        body.update(data)
    return web.json_response(body)


def err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


def make_routes(store, presence):
    """Return route handlers bound to a specific store and presence registry."""

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

        if await presence.deliver(msg["to"], {"type": "msg", "msg": msg}):
            return ok({"delivery": "realtime"})

        queued = await store.enqueue(msg)
        if not queued:
            return err("mailbox full", 429)

        return ok({"delivery": "queued"})

    async def route_fetch(request: web.Request) -> web.Response:
        """Drain pending messages (marks them delivered). Does NOT delete."""
        address = request.match_info["address"]
        token = request.headers.get("X-Flux-Token", "")

        if not validate_token(address, token):
            return err("unauthorized", 401)

        msgs = await store.drain(address)
        return ok({"messages": msgs, "count": len(msgs)})

    async def route_peek(request: web.Request) -> web.Response:
        address = request.match_info["address"]
        token = request.headers.get("X-Flux-Token", "")

        if not validate_token(address, token):
            return err("unauthorized", 401)

        count = await store.peek_count(address)
        return ok({"count": count})

    async def route_inbox(request: web.Request) -> web.Response:
        """
        Return all messages for an address (pending, delivered, read).
        Accepts optional ?status= query param to filter.
        Does not drain — messages are never consumed by this endpoint.
        """
        address = request.match_info["address"]
        token = request.headers.get("X-Flux-Token", "")

        if not validate_token(address, token):
            return err("unauthorized", 401)

        status_filter = request.rel_url.query.get("status")
        msgs = await store.list_messages(address, status=status_filter)
        return ok({"messages": msgs, "count": len(msgs)})

    async def route_read(request: web.Request) -> web.Response:
        """
        Mark a message as read. Requires the recipient's token.
        Only the recipient can mark their own messages as read.
        """
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
        """
        Soft-delete a message. Requires the recipient's token.
        Messages are never physically removed — they are marked 'deleted'.
        Only the recipient can delete their own messages.
        """
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

    async def route_status(request: web.Request) -> web.Response:
        address = request.match_info["address"]
        online = await presence.is_online(address)
        return ok({"address": address, "online": online})

    async def route_stats(request: web.Request) -> web.Response:
        store_stats = await store.stats()
        return ok({
            **store_stats,
            "online_addresses": presence.online_count(),
            "ws_connections": presence.connection_count(),
        })

    async def route_health(request: web.Request) -> web.Response:
        return ok({"version": FLUX_VERSION})

    return [
        web.post("/send", route_send),
        web.get("/fetch/{address}", route_fetch),
        web.get("/peek/{address}", route_peek),
        web.get("/inbox/{address}", route_inbox),
        web.post("/read", route_read),
        web.post("/delete", route_delete),
        web.get("/status/{address}", route_status),
        web.get("/stats", route_stats),
        web.get("/health", route_health),
    ]