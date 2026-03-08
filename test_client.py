#!/usr/bin/env python3
"""
FLUX interactive test client — full featured.
Commands: register, login, send, reply, inbox, inboxes, read, delete, tag, move, me, logout, help, quit
"""

import argparse
import asyncio
import json
from datetime import datetime, timezone
import aiohttp

SERVER = "http://localhost:8765"
SESSION_FILE = ".flux_session.json"
STATUS_ICON = {"pending": "🔵", "delivered": "📬", "read": "✅", "deleted": "🗑️"}


def ts(ms): return datetime.fromtimestamp(ms/1000, tz=timezone.utc).strftime("%Y-%m-%d %H:%M UTC")


def print_msg(msg, index=None):
    status = msg.get("_status", "")
    prefix = f"[{index}] " if index is not None else ""
    subject = msg.get("subject", "(no subject)")
    tags = msg.get("_tags") or []
    inbox = msg.get("_inbox", "inbox")
    print("\n" + "─" * 64)
    print(f"  {prefix}{STATUS_ICON.get(status,'')} [{inbox}]  {subject}")
    print(f"  FROM    : {msg.get('from','?')}")
    print(f"  TO      : {msg.get('to','?')}")
    if msg.get("cc"):
        print(f"  CC      : {', '.join(msg['cc'])}")
    print(f"  ID      : {msg.get('id','?')[:16]}…")
    print(f"  SENT    : {ts(msg.get('t',0))}")
    if msg.get("re"):
        print(f"  REPLY TO: {msg['re'][:16]}…")
    if msg.get("expires") is not None:
        print(f"  EXPIRES : after first read ⚠")
    if tags:
        print(f"  TAGS    : {', '.join(tags)}")
    if msg.get("route"):
        hops = " → ".join(h.get("server","?") for h in msg["route"])
        print(f"  ROUTE   : {hops}")
    print(f"\n  {msg.get('content','')}")
    print("─" * 64)


def save_session(d):
    with open(SESSION_FILE, "w") as f: json.dump(d, f, indent=2)

def load_session():
    try:
        with open(SESSION_FILE) as f: return json.load(f)
    except: return None

def clear_session():
    import os
    try: os.remove(SESSION_FILE)
    except: pass

def resolve(choice, msgs):
    if choice.isdigit():
        i = int(choice)
        return msgs[i]["id"] if 0 <= i < len(msgs) else None
    return next((m["id"] for m in msgs if m["id"].startswith(choice)), None)


async def register(session, server):
    print("\n── Register ──")
    u = input("  Username     : ").strip()
    p = input("  Password     : ").strip()
    dn = input("  Display name : ").strip() or None
    async with session.post(f"{server}/account/register", json={"username":u,"password":p,"display_name":dn}) as r:
        d = await r.json()
    if not d.get("ok"):
        print(f"  Error: {d.get('error')}"); return None
    print(f"  ✓ {d['address']}")
    save_session(d); return d


async def login(session, server):
    print("\n── Login ──")
    u = input("  Username : ").strip()
    p = input("  Password : ").strip()
    async with session.post(f"{server}/account/login", json={"username":u,"password":p}) as r:
        d = await r.json()
    if not d.get("ok"):
        print(f"  Error: {d.get('error')}"); return None
    print(f"  ✓ {d['address']}")
    save_session(d); return d


async def send_mail(session, server, account, reply_to_id=None):
    print("\n── Send ──")
    to = input("  To            : ").strip()
    subject = input("  Subject       : ").strip() or None
    cc_raw = input("  CC            : ").strip()
    bcc_raw = input("  BCC           : ").strip()
    tags_raw = input("  Tags          : ").strip()
    auto_del = input("  Delete after read? (y/N): ").strip().lower() == "y"
    print("  Message (blank line to send):")
    lines = []
    while True:
        line = input()
        if line == "": break
        lines.append(line)
    content = "\n".join(lines)
    if not content.strip():
        print("  Cancelled."); return

    payload = {"to": to, "content": content}
    if subject: payload["subject"] = subject
    if reply_to_id: payload["reply_to"] = reply_to_id
    if cc_raw: payload["cc"] = [a.strip() for a in cc_raw.split(",") if a.strip()]
    if bcc_raw: payload["bcc"] = [a.strip() for a in bcc_raw.split(",") if a.strip()]
    if tags_raw: payload["tags"] = [t.strip() for t in tags_raw.split(",") if t.strip()]
    if auto_del: payload["expires"] = 0

    async with session.post(f"{server}/mail/send", json=payload, headers={"X-Flux-Session": account["session"]}) as r:
        d = await r.json()
    if d.get("ok"):
        print(f"  ✓ Sent  delivery={d.get('delivery')}  id={d.get('id','')[:16]}…")
        for addr, res in (d.get("cc") or {}).items(): print(f"    CC  {addr}: {res}")
        for addr, res in (d.get("bcc") or {}).items(): print(f"    BCC {addr}: {res}")
    else:
        print(f"  Error: {d.get('error')}")


async def fetch_inbox(session, server, account, inbox="inbox"):
    print(f"\n── Inbox: {inbox} ──")
    sf = input("  Status filter (blank=all): ").strip() or None
    tf = input("  Tag filter    (blank=none): ").strip() or None
    params = {"inbox": inbox}
    if sf: params["status"] = sf
    if tf: params["tag"] = tf
    async with session.get(f"{server}/mail/inbox", headers={"X-Flux-Session": account["session"]}, params=params) as r:
        d = await r.json()
    if not d.get("ok"):
        print(f"  Error: {d.get('error')}"); return []
    msgs = d.get("messages", [])
    if not msgs: print("  Empty.")
    else:
        print(f"  {len(msgs)} message(s):")
        for i, m in enumerate(msgs): print_msg(m, index=i)
    return msgs


async def list_inboxes(session, server, account):
    async with session.get(f"{server}/mail/inboxes", headers={"X-Flux-Session": account["session"]}) as r:
        d = await r.json()
    if d.get("ok"): print("  Inboxes: " + ", ".join(d.get("inboxes", [])))


async def action_on_msg(session, server, account, msgs, route, payload_fn, ok_msg):
    if not msgs: print("  Run 'inbox' first."); return
    choice = input("  Index or ID: ").strip()
    mid = resolve(choice, msgs)
    if not mid: print("  Not found."); return
    async with session.post(f"{server}{route}", json=payload_fn(mid), headers={"X-Flux-Session": account["session"]}) as r:
        d = await r.json()
    print(f"  {ok_msg}" if d.get("ok") else f"  Error: {d.get('error')}")


async def show_me(session, server, account):
    async with session.get(f"{server}/account/me", headers={"X-Flux-Session": account["session"]}) as r:
        d = await r.json()
    if d.get("ok"):
        print(f"\n  {d['username']}  {d['address']}  {d.get('display_name') or ''}")


async def ws_loop(server, account):
    ws_url = server.replace("http://","ws://").replace("https://","wss://") + "/ws"
    from flux.auth import derive_token
    token = derive_token(account["flux_address"])
    async with aiohttp.ClientSession() as s:
        try:
            ws = await s.ws_connect(ws_url)
            await ws.send_str(json.dumps({"action":"auth","address":account["flux_address"],"token":token}))
            async for raw in ws:
                if raw.type == aiohttp.WSMsgType.TEXT:
                    frame = json.loads(raw.data)
                    if frame.get("action") == "authed":
                        unread = [m for m in frame.get("messages",[]) if m.get("_status") in ("pending","delivered")]
                        if unread:
                            print(f"\n  ✉  {len(unread)} unread:")
                            for m in unread: print_msg(m)
                        else: print("  ✓  Connected.")
                    elif frame.get("type") == "msg":
                        print("\n  ✉  New:"); print_msg(frame["msg"]); print("\n> ", end="", flush=True)
                elif raw.type in (aiohttp.WSMsgType.CLOSE, aiohttp.WSMsgType.ERROR): break
        except Exception as e: print(f"\n  WS disconnected: {e}")


async def poll_loop(server, account, interval=5):
    async with aiohttp.ClientSession() as s:
        while True:
            await asyncio.sleep(interval)
            try:
                async with s.get(f"{server}/mail/inbox", headers={"X-Flux-Session": account["session"]}, params={"status":"pending"}) as r:
                    d = await r.json()
                msgs = d.get("messages",[])
                if msgs:
                    print(f"\n  ✉  {len(msgs)} new:"); [print_msg(m) for m in msgs]; print("\n> ", end="", flush=True)
            except: pass


def print_help(account):
    print("\n  Commands:")
    if not account:
        print("    register / login")
    else:
        print("    send  reply  inbox  inboxes  read  delete  tag  move  me  logout")
    print("    help  quit")


async def main_loop(server, use_http):
    account = load_session()
    bg_task = None
    last_msgs = []
    cur_inbox = "inbox"

    async with aiohttp.ClientSession() as session:
        if account:
            print(f"  Resumed as {account.get('address', account.get('username'))}")
            bg_task = asyncio.create_task(poll_loop(server, account) if use_http else ws_loop(server, account))

        print_help(account)
        loop = asyncio.get_event_loop()

        while True:
            try:
                cmd = await loop.run_in_executor(None, lambda: input("\n> ").strip().lower())
            except (EOFError, KeyboardInterrupt):
                break

            if cmd in ("quit","exit","q"): break
            elif cmd == "help": print_help(account)
            elif cmd in ("register","login"):
                if account: print("  Already logged in."); continue
                account = await (register if cmd=="register" else login)(session, server)
                if account:
                    if bg_task: bg_task.cancel()
                    bg_task = asyncio.create_task(poll_loop(server,account) if use_http else ws_loop(server,account))
            elif cmd == "send":
                if not account: print("  Not logged in."); continue
                await send_mail(session, server, account)
            elif cmd == "reply":
                if not account: print("  Not logged in."); continue
                if not last_msgs: print("  Run 'inbox' first."); continue
                choice = input("  Index or ID to reply to: ").strip()
                mid = resolve(choice, last_msgs)
                if mid:
                    orig = next((m for m in last_msgs if m["id"] == mid), None)
                    if orig: print(f"  Replying to {orig.get('from','?')}…")
                    await send_mail(session, server, account, reply_to_id=mid)
            elif cmd == "inbox":
                if not account: print("  Not logged in."); continue
                inp = input(f"  Inbox (blank='{cur_inbox}'): ").strip()
                if inp: cur_inbox = inp
                last_msgs = await fetch_inbox(session, server, account, inbox=cur_inbox)
            elif cmd == "inboxes":
                if not account: print("  Not logged in."); continue
                await list_inboxes(session, server, account)
            elif cmd == "read":
                if not account: print("  Not logged in."); continue
                await action_on_msg(session, server, account, last_msgs, "/mail/read",
                    lambda mid: {"id": mid}, "✅ Marked as read.")
            elif cmd == "delete":
                if not account: print("  Not logged in."); continue
                await action_on_msg(session, server, account, last_msgs, "/mail/delete",
                    lambda mid: {"id": mid}, "🗑️  Soft-deleted.")
            elif cmd == "tag":
                if not account: print("  Not logged in."); continue
                if not last_msgs: print("  Run 'inbox' first."); continue
                choice = input("  Index or ID: ").strip()
                mid = resolve(choice, last_msgs)
                if not mid: print("  Not found."); continue
                tag = input("  Tag (important/favorited/custom): ").strip().lower()
                act = input("  Action (add/remove, default add): ").strip() or "add"
                async with session.post(f"{server}/mail/tag", json={"id":mid,"tag":tag,"action":act},
                    headers={"X-Flux-Session": account["session"]}) as r:
                    d = await r.json()
                print(f"  🏷️  {tag} {act}ed." if d.get("ok") else f"  Error: {d.get('error')}")
            elif cmd == "move":
                if not account: print("  Not logged in."); continue
                if not last_msgs: print("  Run 'inbox' first."); continue
                choice = input("  Index or ID: ").strip()
                mid = resolve(choice, last_msgs)
                if not mid: print("  Not found."); continue
                dest = input("  Move to inbox: ").strip()
                async with session.post(f"{server}/mail/move", json={"id":mid,"inbox":dest},
                    headers={"X-Flux-Session": account["session"]}) as r:
                    d = await r.json()
                print(f"  📁 Moved to '{dest}'." if d.get("ok") else f"  Error: {d.get('error')}")
            elif cmd == "me":
                if not account: print("  Not logged in."); continue
                await show_me(session, server, account)
            elif cmd == "logout":
                if not account: print("  Not logged in."); continue
                async with session.post(f"{server}/account/logout", headers={"X-Flux-Session": account["session"]}) as r:
                    pass
                clear_session(); account = None; last_msgs = []
                if bg_task: bg_task.cancel(); bg_task = None
                print("  Logged out.")
            else:
                print(f"  Unknown: '{cmd}'"); print_help(account)

    if bg_task: bg_task.cancel()


def main():
    p = argparse.ArgumentParser()
    p.add_argument("--server", default=SERVER)
    p.add_argument("--http", action="store_true")
    args = p.parse_args()
    print(f"FLUX  →  {args.server}\nType 'help'.")
    asyncio.run(main_loop(args.server, args.http))

if __name__ == "__main__":
    main()