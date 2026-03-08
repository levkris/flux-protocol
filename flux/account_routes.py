import logging

import aiohttp
from aiohttp import web

from .constants import FLUX_VERSION, FLUX_API_VERSION, DEFAULT_INBOX
from .federation import parse_federated, is_local, resolve_address
from .message import build_message, validate_fields, verify_message, check_freshness, now_ms, strip_bcc, append_route
from .spam import is_spam
from .integrity import append_integrity_hop

log = logging.getLogger("flux.account_routes")


def _ok(data: dict | None = None) -> web.Response:
    body = {"ok": True}
    if data:
        body.update(data)
    return web.json_response(body)


def _err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


def _require_session(handler):
    async def wrapper(request: web.Request):
        token = request.headers.get("X-Flux-Session", "")
        if not token:
            return _err("missing X-Flux-Session header", 401)
        username = await request.app["accounts"].validate_session(token)
        if not username:
            return _err("invalid or expired session", 401)
        request["username"] = username
        return await handler(request)
    return wrapper


def make_account_routes(domain: str):

    async def route_register(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _err("invalid json")

        username = body.get("username", "").strip().lower()
        password = body.get("password", "")
        display_name = body.get("display_name")

        if not username or len(username) < 2 or len(username) > 32:
            return _err("username must be 2–32 characters")
        if not all(c.isalnum() or c in ("-", "_") for c in username):
            return _err("username may only contain letters, numbers, - and _")
        if not password or len(password) < 8:
            return _err("password must be at least 8 characters")

        try:
            account = await request.app["accounts"].create_account(
                username=username, display_name=display_name, password=password,
            )
        except ValueError as e:
            return _err(str(e), 409)

        token = await request.app["accounts"].auth_password(username, password)
        return _ok({
            "username": username,
            "address": f"{username}@{domain}",
            "flux_address": account["address"],
            "session": token,
        })

    async def route_login(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _err("invalid json")

        username = body.get("username", "").strip().lower()
        password = body.get("password", "")

        token = await request.app["accounts"].auth_password(username, password)
        if not token:
            return _err("invalid credentials", 401)

        account = await request.app["accounts"].get_by_username(username)
        return _ok({
            "session": token,
            "username": username,
            "address": f"{username}@{domain}",
            "flux_address": account["flux_address"],
        })

    async def route_logout(request: web.Request) -> web.Response:
        token = request.headers.get("X-Flux-Session", "")
        if token:
            await request.app["accounts"].revoke_session(token)
        return _ok()

    @_require_session
    async def route_me(request: web.Request) -> web.Response:
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        return _ok({
            "username": username,
            "address": f"{username}@{domain}",
            "flux_address": account["flux_address"],
            "display_name": account["display_name"],
        })

    @_require_session
    async def route_federated_send(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _err("invalid json")

        to_address = body.get("to", "").strip()
        content = body.get("content", "")
        subject = body.get("subject")
        reply_to = body.get("reply_to") or body.get("re")
        cc_raw: list[str] = body.get("cc") or []
        bcc_raw: list[str] = body.get("bcc") or []
        tags: list[str] = body.get("tags") or []
        expires = body.get("expires")

        if not to_address or not content:
            return _err("missing 'to' or 'content'")

        sender_username = request["username"]
        identity = await request.app["accounts"].get_identity(sender_username)
        if not identity:
            return _err("sender identity not found", 500)

        store = request.app["store"]
        presence = request.app["presence"]

        async def resolve(addr: str) -> str | None:
            parsed = parse_federated(addr)
            if parsed is None:
                return addr
            if is_local(addr, domain):
                profile = await request.app["accounts"].get_public_profile(parsed[0])
                return profile["flux_address"] if profile else None
            async with aiohttp.ClientSession() as s:
                return await resolve_address(addr, domain, s)

        async def deliver_one(fx1_to: str, msg: dict) -> str:
            msg = append_integrity_hop(append_route(msg, domain), domain)
            msg_clean = strip_bcc(msg)

            spam_result = is_spam(msg_clean)
            inbox = "spam" if spam_result["spam"] else DEFAULT_INBOX
            if spam_result["spam"]:
                log.info(f"spam from {msg_clean.get('from','?')} routed to spam inbox: {spam_result['reason']}")

            # Spam is never pushed in realtime — always stored in the spam inbox
            if not spam_result["spam"] and await presence.deliver(fx1_to, {"type": "msg", "msg": msg_clean}):
                return "realtime"
            queued = await store.enqueue(msg_clean, inbox=inbox)
            return "queued" if queued else "dropped"

        fx1_to = await resolve(to_address)
        if not fx1_to:
            return _err(f"could not resolve: {to_address}", 404)

        msg = build_message(
            identity, fx1_to, content,
            subject=subject, reply_to=reply_to,
            cc=cc_raw, bcc=bcc_raw,
            tags=tags, expires=expires,
        )
        primary_delivery = await deliver_one(fx1_to, msg)

        cc_results = {}
        for cc_addr in cc_raw:
            fx1_cc = await resolve(cc_addr)
            if fx1_cc:
                cc_msg = build_message(
                    identity, fx1_cc, content,
                    subject=subject, reply_to=reply_to,
                    cc=cc_raw, tags=tags, expires=expires,
                )
                cc_results[cc_addr] = await deliver_one(fx1_cc, cc_msg)
            else:
                cc_results[cc_addr] = "unresolved"

        bcc_results = {}
        for bcc_addr in bcc_raw:
            fx1_bcc = await resolve(bcc_addr)
            if fx1_bcc:
                bcc_msg = build_message(
                    identity, fx1_bcc, content,
                    subject=subject, reply_to=reply_to,
                    cc=cc_raw, tags=tags, expires=expires,
                )
                bcc_results[bcc_addr] = await deliver_one(fx1_bcc, bcc_msg)
            else:
                bcc_results[bcc_addr] = "unresolved"

        sender_display = await _sender_display(request.app["accounts"], sender_username, domain)

        return _ok({
            "id": msg["id"],
            "from": f"{sender_username}@{domain}",
            "from_display": sender_display,
            "to": to_address,
            "fx1_to": fx1_to,
            "delivery": primary_delivery,
            "cc": cc_results,
            "bcc": bcc_results,
        })

    @_require_session
    async def route_fetch_inbox(request: web.Request) -> web.Response:
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        fx1_address = account["flux_address"]

        inbox = request.rel_url.query.get("inbox", DEFAULT_INBOX)
        status_filter = request.rel_url.query.get("status")
        tag_filter = request.rel_url.query.get("tag")

        await request.app["store"].drain(fx1_address, inbox)
        msgs = await request.app["store"].list_messages(fx1_address, inbox, status=status_filter, tag=tag_filter)
        return _ok({"messages": msgs, "count": len(msgs)})

    @_require_session
    async def route_list_inboxes(request: web.Request) -> web.Response:
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        inboxes = await request.app["store"].list_inboxes(account["flux_address"])
        if DEFAULT_INBOX not in inboxes:
            inboxes = [DEFAULT_INBOX] + inboxes
        return _ok({"inboxes": inboxes})

    @_require_session
    async def route_peek_inbox(request: web.Request) -> web.Response:
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        inbox = request.rel_url.query.get("inbox", DEFAULT_INBOX)
        count = await request.app["store"].peek_count(account["flux_address"], inbox)
        return _ok({"count": count})

    @_require_session
    async def route_read_message(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _err("invalid json")
        msg_id = body.get("id", "")
        if not msg_id:
            return _err("missing 'id'")
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        found = await request.app["store"].mark_read(msg_id, account["flux_address"])
        if not found:
            return _err("message not found or already read", 404)
        return _ok({"read": True})

    @_require_session
    async def route_delete_message(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _err("invalid json")
        msg_id = body.get("id", "")
        if not msg_id:
            return _err("missing 'id'")
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        found = await request.app["store"].delete_message(msg_id, account["flux_address"])
        if not found:
            return _err("message not found or already deleted", 404)
        return _ok({"deleted": True})

    @_require_session
    async def route_tag_message(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _err("invalid json")
        msg_id = body.get("id", "")
        tag = body.get("tag", "").strip().lower()
        action = body.get("action", "add")
        if not msg_id or not tag:
            return _err("missing 'id' or 'tag'")
        if action not in ("add", "remove"):
            return _err("action must be 'add' or 'remove'")
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        fx1 = account["flux_address"]
        if action == "add":
            found = await request.app["store"].add_tag(msg_id, fx1, tag)
        else:
            found = await request.app["store"].remove_tag(msg_id, fx1, tag)
        return _ok({"tag": tag, "action": action, "applied": found})

    @_require_session
    async def route_move_message(request: web.Request) -> web.Response:
        try:
            body = await request.json()
        except Exception:
            return _err("invalid json")
        msg_id = body.get("id", "")
        inbox = body.get("inbox", "").strip()
        if not msg_id or not inbox:
            return _err("missing 'id' or 'inbox'")
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        found = await request.app["store"].move_inbox(msg_id, account["flux_address"], inbox)
        return _ok({"moved": found, "inbox": inbox})

    async def route_federation_resolve(request: web.Request) -> web.Response:
        username = request.match_info["username"].lower()
        profile = await request.app["accounts"].get_public_profile(username)
        if not profile:
            return _err(f"user '{username}' not found", 404)
        return _ok(profile)

    async def route_federation_info(request: web.Request) -> web.Response:
        return _ok({"domain": domain, "version": FLUX_API_VERSION, "protocol": FLUX_VERSION, "federation": True})

    return [
        web.post("/account/register", route_register),
        web.post("/account/login", route_login),
        web.post("/account/logout", route_logout),
        web.get("/account/me", route_me),
        web.post("/mail/send", route_federated_send),
        web.get("/mail/inbox", route_fetch_inbox),
        web.get("/mail/inboxes", route_list_inboxes),
        web.get("/mail/peek", route_peek_inbox),
        web.post("/mail/read", route_read_message),
        web.post("/mail/delete", route_delete_message),
        web.post("/mail/tag", route_tag_message),
        web.post("/mail/move", route_move_message),
        web.get("/federation/resolve/{username}", route_federation_resolve),
        web.get("/federation/info", route_federation_info),
    ]


async def _sender_display(accounts, username: str, domain: str) -> str:
    account = await accounts.get_by_username(username)
    if account and account.get("display_name"):
        return f"{account['display_name']} <{username}@{domain}>"
    return f"{username}@{domain}"