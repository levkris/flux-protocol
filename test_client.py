#!/usr/bin/env python3
"""
FLUX interactive test client.

Connects via WebSocket by default. Supports the full account system:
  - Register with username + password
  - Login
  - Send mail to user@domain or fx1... addresses
  - Receive mail in real time
  - Fetch inbox (HTTP fallback)

Usage:
    python test_client.py                          # connect to localhost:8765
    python test_client.py --server http://mynode.com:8765
    python test_client.py --http                   # use HTTP polling instead of WebSocket
"""

import argparse
import asyncio
import json
import sys
from datetime import datetime, timezone

import aiohttp

SERVER = "http://localhost:8765"
SESSION_FILE = ".flux_session.json"


def ts_to_str(ts_ms: int) -> str:
    dt = datetime.fromtimestamp(ts_ms / 1000, tz=timezone.utc)
    return dt.strftime("%Y-%m-%d %H:%M:%S UTC")


def print_msg(msg: dict):
    print("\n" + "─" * 60)
    print(f"  FROM : {msg.get('from', '?')}")
    print(f"  TO   : {msg.get('to', '?')}")
    print(f"  ID   : {msg.get('id', '?')[:16]}…")
    print(f"  TIME : {ts_to_str(msg.get('t', 0))}")
    if msg.get("re"):
        print(f"  RE   : {msg['re'][:16]}…")
    print()
    print(f"  {msg.get('content', '')}")
    print("─" * 60)


def save_session(data: dict):
    with open(SESSION_FILE, "w") as f:
        json.dump(data, f, indent=2)


def load_session() -> dict | None:
    try:
        with open(SESSION_FILE) as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return None


def clear_session():
    import os
    try:
        os.remove(SESSION_FILE)
    except FileNotFoundError:
        pass


async def register(session: aiohttp.ClientSession, server: str) -> dict | None:
    print("\n── Register new account ──")
    username = input("  Username       : ").strip()
    password = input("  Password       : ").strip()
    display_name = input("  Display name   : ").strip() or None

    async with session.post(f"{server}/account/register", json={
        "username": username,
        "password": password,
        "display_name": display_name,
    }) as r:
        data = await r.json()

    if not data.get("ok"):
        print(f"\n  Error: {data.get('error')}")
        return None

    print(f"\n  Account created!")
    print(f"  Address  : {data['address']}")
    print(f"  FLUX key : {data['flux_address']}")
    save_session(data)
    return data


async def login(session: aiohttp.ClientSession, server: str) -> dict | None:
    print("\n── Login ──")
    username = input("  Username : ").strip()
    password = input("  Password : ").strip()

    async with session.post(f"{server}/account/login", json={
        "username": username,
        "password": password,
    }) as r:
        data = await r.json()

    if not data.get("ok"):
        print(f"\n  Error: {data.get('error')}")
        return None

    print(f"\n  Logged in as {data['address']}")
    save_session(data)
    return data


async def send_mail(session: aiohttp.ClientSession, server: str, account: dict):
    print("\n── Send mail ──")
    to = input("  To (user@domain or fx1...) : ").strip()
    print("  Message (blank line to send):")
    lines = []
    while True:
        line = input()
        if line == "":
            break
        lines.append(line)
    content = "\n".join(lines)

    if not content.strip():
        print("  (empty message, cancelled)")
        return

    async with session.post(
        f"{server}/mail/send",
        json={"to": to, "content": content},
        headers={"X-Flux-Session": account["session"]},
    ) as r:
        data = await r.json()

    if data.get("ok"):
        print(f"\n  Sent! delivery={data.get('delivery')}  id={data.get('id', '')[:16]}…")
    else:
        print(f"\n  Error: {data.get('error')}")


async def fetch_inbox(session: aiohttp.ClientSession, server: str, account: dict):
    async with session.get(
        f"{server}/mail/inbox",
        headers={"X-Flux-Session": account["session"]},
    ) as r:
        data = await r.json()

    if not data.get("ok"):
        print(f"  Error: {data.get('error')}")
        return

    msgs = data.get("messages", [])
    if not msgs:
        print("\n  Inbox empty.")
    else:
        print(f"\n  {len(msgs)} message(s):")
        for msg in msgs:
            print_msg(msg)


async def show_me(session: aiohttp.ClientSession, server: str, account: dict):
    async with session.get(
        f"{server}/account/me",
        headers={"X-Flux-Session": account["session"]},
    ) as r:
        data = await r.json()

    if data.get("ok"):
        print(f"\n  Username     : {data['username']}")
        print(f"  Address      : {data['address']}")
        print(f"  FLUX address : {data['flux_address']}")
        print(f"  Display name : {data.get('display_name') or '(none)'}")


# ── WebSocket real-time mode ──────────────────────────────────────────────────

async def ws_loop(server: str, account: dict):
    """
    Maintain a WebSocket connection for real-time message delivery.
    Runs as a background task while the interactive prompt is alive.
    """
    ws_url = server.replace("http://", "ws://").replace("https://", "wss://") + "/ws"
    flux_address = account["flux_address"]

    from flux.auth import derive_token
    token = derive_token(flux_address)

    async with aiohttp.ClientSession() as session:
        try:
            ws = await session.ws_connect(ws_url)
            await ws.send_str(json.dumps({
                "action": "auth",
                "address": flux_address,
                "token": token,
            }))

            async for raw in ws:
                if raw.type == aiohttp.WSMsgType.TEXT:
                    frame = json.loads(raw.data)

                    if frame.get("action") == "authed":
                        queued = frame.get("queued", [])
                        if queued:
                            print(f"\n  ✉  {len(queued)} queued message(s) delivered:")
                            for msg in queued:
                                print_msg(msg)
                        else:
                            print("  ✓  WebSocket connected. Waiting for messages…")

                    elif frame.get("type") == "msg":
                        print("\n  ✉  New message:")
                        print_msg(frame["msg"])
                        print(f"\n> ", end="", flush=True)

                elif raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR):
                    break

        except Exception as e:
            print(f"\n  WebSocket disconnected: {e}")


# ── HTTP polling mode ─────────────────────────────────────────────────────────

async def poll_loop(server: str, account: dict, interval: int = 5):
    """Poll the inbox every `interval` seconds (HTTP fallback)."""
    async with aiohttp.ClientSession() as session:
        while True:
            await asyncio.sleep(interval)
            try:
                async with session.get(
                    f"{server}/mail/inbox",
                    headers={"X-Flux-Session": account["session"]},
                ) as r:
                    data = await r.json()
                msgs = data.get("messages", [])
                if msgs:
                    print(f"\n  ✉  {len(msgs)} new message(s):")
                    for msg in msgs:
                        print_msg(msg)
                    print(f"\n> ", end="", flush=True)
            except Exception:
                pass


# ── Main interactive loop ─────────────────────────────────────────────────────

def print_help(account: dict | None):
    print("\n  Commands:")
    if not account:
        print("    register   — create a new account")
        print("    login      — log in to existing account")
    else:
        print(f"    send       — send a message")
        print(f"    inbox      — fetch inbox (HTTP)")
        print(f"    me         — show your account info")
        print(f"    logout     — log out")
    print("    help       — show this")
    print("    quit       — exit")


async def main_loop(server: str, use_http: bool):
    account = load_session()
    bg_task = None

    async with aiohttp.ClientSession() as session:

        if account:
            print(f"\n  Resumed session as {account.get('address', account.get('username'))}")
            if not use_http:
                bg_task = asyncio.create_task(ws_loop(server, account))
            else:
                print(f"  HTTP polling mode (every 5s)")
                bg_task = asyncio.create_task(poll_loop(server, account))

        print_help(account)

        loop = asyncio.get_event_loop()

        while True:
            try:
                cmd = await loop.run_in_executor(None, lambda: input("\n> ").strip().lower())
            except (EOFError, KeyboardInterrupt):
                break

            if cmd in ("quit", "exit", "q"):
                break

            elif cmd == "help":
                print_help(account)

            elif cmd == "register":
                if account:
                    print("  Already logged in. Logout first.")
                    continue
                account = await register(session, server)
                if account:
                    if bg_task:
                        bg_task.cancel()
                    if not use_http:
                        bg_task = asyncio.create_task(ws_loop(server, account))
                    else:
                        bg_task = asyncio.create_task(poll_loop(server, account))

            elif cmd == "login":
                if account:
                    print("  Already logged in. Logout first.")
                    continue
                account = await login(session, server)
                if account:
                    if bg_task:
                        bg_task.cancel()
                    if not use_http:
                        bg_task = asyncio.create_task(ws_loop(server, account))
                    else:
                        bg_task = asyncio.create_task(poll_loop(server, account))

            elif cmd == "send":
                if not account:
                    print("  Not logged in.")
                    continue
                await send_mail(session, server, account)

            elif cmd == "inbox":
                if not account:
                    print("  Not logged in.")
                    continue
                await fetch_inbox(session, server, account)

            elif cmd == "me":
                if not account:
                    print("  Not logged in.")
                    continue
                await show_me(session, server, account)

            elif cmd == "logout":
                if not account:
                    print("  Not logged in.")
                    continue
                async with session.post(
                    f"{server}/account/logout",
                    headers={"X-Flux-Session": account["session"]},
                ) as r:
                    pass
                clear_session()
                account = None
                if bg_task:
                    bg_task.cancel()
                    bg_task = None
                print("  Logged out.")

            else:
                print(f"  Unknown command: '{cmd}'")
                print_help(account)

    if bg_task:
        bg_task.cancel()


def main():
    parser = argparse.ArgumentParser(description="FLUX interactive mail client")
    parser.add_argument("--server", default=SERVER, help="FLUX server URL")
    parser.add_argument("--http", action="store_true", help="Use HTTP polling instead of WebSocket")
    args = parser.parse_args()

    print(f"FLUX client  →  {args.server}")
    print("Type 'help' for commands.")

    asyncio.run(main_loop(args.server, args.http))


if __name__ == "__main__":
    main()
