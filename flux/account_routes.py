import logging

import aiohttp
from aiohttp import web

from .constants import FLUX_VERSION
from .federation import parse_federated, is_local, resolve_address
from .message import build_message, validate_fields, verify_message, check_freshness, now_ms

log = logging.getLogger("flux.account_routes")


def _ok(data: dict | None = None) -> web.Response:
    body = {"ok": True}
    if data:
        body.update(data)
    return web.json_response(body)


def _err(msg: str, status: int = 400) -> web.Response:
    return web.json_response({"ok": False, "error": msg}, status=status)


def _require_session(handler):
    """Decorator that validates X-Flux-Session and injects username into request."""
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
    """
    Returns account + federation route handlers bound to a domain name.
    The domain is used to determine which addresses are local vs remote.
    """

    # --- Account management ---

    async def route_register(request: web.Request) -> web.Response:
        """Create a new account with password auth."""
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
                username=username,
                display_name=display_name,
                password=password,
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
        """Authenticate with username + password. Returns a session token."""
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
        """Return the current user's profile."""
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        return _ok({
            "username": username,
            "address": f"{username}@{domain}",
            "flux_address": account["flux_address"],
            "display_name": account["display_name"],
        })

    # --- Federated send ---

    @_require_session
    async def route_federated_send(request: web.Request) -> web.Response:
        """
        Send a message using federated addressing (user@domain).

        The server:
        1. Looks up the sender's FLUX identity
        2. Resolves the recipient address (local lookup or remote federation query)
        3. Signs and delivers the message
        """
        try:
            body = await request.json()
        except Exception:
            return _err("invalid json")

        to_address = body.get("to", "").strip()
        content = body.get("content", "")
        reply_to = body.get("reply_to")

        if not to_address or not content:
            return _err("missing 'to' or 'content'")

        sender_username = request["username"]
        identity = await request.app["accounts"].get_identity(sender_username)
        if not identity:
            return _err("sender identity not found", 500)

        # Resolve recipient to a raw fx1 address
        parsed = parse_federated(to_address)

        if parsed is None:
            # Already a raw fx1 address
            fx1_address = to_address
        elif is_local(to_address, domain):
            recipient_username = parsed[0]
            profile = await request.app["accounts"].get_public_profile(recipient_username)
            if not profile:
                return _err(f"user '{recipient_username}' not found on this server", 404)
            fx1_address = profile["flux_address"]
        else:
            # Remote — ask the other server for their address
            async with aiohttp.ClientSession() as session:
                fx1_address = await resolve_address(to_address, domain, session)
            if not fx1_address:
                return _err(f"could not resolve remote address: {to_address}", 404)

        msg = build_message(identity, fx1_address, content, reply_to)

        # Attempt real-time delivery first, then queue
        store = request.app["store"]
        presence = request.app["presence"]

        if await presence.deliver(fx1_address, {"type": "msg", "msg": msg}):
            delivery = "realtime"
        else:
            queued = await store.enqueue(msg)
            delivery = "queued" if queued else "dropped"

        sender_display = await _sender_display(request.app["accounts"], sender_username, domain)

        return _ok({
            "id": msg["id"],
            "from": f"{sender_username}@{domain}",
            "from_display": sender_display,
            "to": to_address,
            "fx1_to": fx1_address,
            "delivery": delivery,
        })

    @_require_session
    async def route_fetch_inbox(request: web.Request) -> web.Response:
        """Fetch and drain messages for the authenticated user."""
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        fx1_address = account["flux_address"]

        msgs = await request.app["store"].drain(fx1_address)
        return _ok({"messages": msgs, "count": len(msgs)})

    @_require_session
    async def route_peek_inbox(request: web.Request) -> web.Response:
        username = request["username"]
        account = await request.app["accounts"].get_by_username(username)
        fx1_address = account["flux_address"]

        count = await request.app["store"].peek_count(fx1_address)
        return _ok({"count": count})

    # --- Federation endpoint (called by remote servers) ---

    async def route_federation_resolve(request: web.Request) -> web.Response:
        """
        Federation endpoint. Remote servers call this to look up a local user's
        FLUX address before sending them a message.

        Returns the public profile only — no private key, no session info.
        """
        username = request.match_info["username"].lower()
        profile = await request.app["accounts"].get_public_profile(username)
        if not profile:
            return _err(f"user '{username}' not found", 404)
        return _ok(profile)

    async def route_federation_info(request: web.Request) -> web.Response:
        """Returns metadata about this FLUX node for discovery."""
        return _ok({
            "domain": domain,
            "version": FLUX_VERSION,
            "federation": True,
        })

    return [
        web.post("/account/register", route_register),
        web.post("/account/login", route_login),
        web.post("/account/logout", route_logout),
        web.get("/account/me", route_me),
        web.post("/mail/send", route_federated_send),
        web.get("/mail/inbox", route_fetch_inbox),
        web.get("/mail/peek", route_peek_inbox),
        web.get("/federation/resolve/{username}", route_federation_resolve),
        web.get("/federation/info", route_federation_info),
    ]


async def _sender_display(accounts, username: str, domain: str) -> str:
    account = await accounts.get_by_username(username)
    if account and account.get("display_name"):
        return f"{account['display_name']} <{username}@{domain}>"
    return f"{username}@{domain}"
