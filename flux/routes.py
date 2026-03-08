import logging

from aiohttp import web

from .auth import validate_token
from .constants import FLUX_VERSION, FLUX_API_VERSION
from .message import validate_fields, verify_message, check_freshness, append_route, strip_bcc
from .store import DEFAULT_INBOX

log = logging.getLogger("flux.routes")


def ok(data: dict | None = None) -> web.Response:
    body = {"ok": True}
    if data:
        body.update(data)
    return web.json_response(body)


def err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


def make_routes(store, presence, domain: str = "", mesh_relay=None):
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

        # Append this server to the route
        hop = domain or request.host
        msg = append_route(msg, hop)

        # Strip BCC before delivery/storage
        msg_clean = strip_bcc(msg)

        if await presence.deliver(msg_clean["to"], {"type": "msg", "msg": msg_clean}):
            delivery = "realtime"
        else:
            queued = await store.enqueue(msg_clean)
            if not queued:
                return err("mailbox full", 429)
            delivery = "queued"

        # Fan out to mesh peers if configured
        mesh_results = {}
        if mesh_relay:
            mesh_results = await mesh_relay.relay(msg_clean)

        return ok({"delivery": delivery, "mesh": mesh_results})

    async def route_fetch(request: web.Request) -> web.Response:
        """Drain pending messages (marks them delivered). Does NOT delete."""
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
        """
        Return all messages for an address without consuming them.
        Query params: ?inbox=  ?status=  ?tag=
        """
        address = request.match_info["address"]
        token = request.headers.get("X-Flux-Token", "")
        if not validate_token(address, token):
            return err("unauthorized", 401)

        inbox = request.rel_url.query.get("inbox", DEFAULT_INBOX)
        status_filter = request.rel_url.query.get("status")
        tag_filter = request.rel_url.query.get("tag")

        # Drain pending → delivered so inbox reflects current state
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
        """Mark a message as read. Only the recipient can do this."""
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
        """Soft-delete a message. Only the recipient can do this."""
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
        """Add or remove a tag. Body: {id, address, tag, action: 'add'|'remove'}"""
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
        """Move a message to a different inbox. Body: {id, address, inbox}"""
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
        return ok({
            **store_stats,
            "online_addresses": presence.online_count(),
            "ws_connections": presence.connection_count(),
        })

    async def route_health(request: web.Request) -> web.Response:
        return ok({"protocol": FLUX_VERSION, "version": FLUX_API_VERSION})

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
    ]