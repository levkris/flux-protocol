#!/usr/bin/env python3
import argparse
import asyncio
import json
import logging
import os
import subprocess
import sys

logging.basicConfig(level=logging.INFO, format="%(asctime)s [FLUX] %(message)s")
log = logging.getLogger("flux")


def cmd_server(args):
    if args.detach:
        pid_file = f"flux_{args.port}.pid"
        cmd = [sys.executable, __file__, "server", "--port", str(args.port), "--backend", args.backend]
        if args.db:
            cmd += ["--db", args.db]
        if args.domain:
            cmd += ["--domain", args.domain]
        with open("flux.log", "a") as lf:
            proc = subprocess.Popen(cmd, stdout=lf, stderr=lf, start_new_session=True)
        with open(pid_file, "w") as f:
            f.write(str(proc.pid))
        print(f"FLUX server started  port={args.port}  pid={proc.pid}")
        print(f"  log → flux.log")
        print(f"  pid → {pid_file}")
        print(f"  stop → kill $(cat {pid_file})")
        return

    from flux.server import run_server
    run_server(
        host=args.host,
        port=args.port,
        backend=args.backend,
        db_path=args.db or "flux.db",
        accounts_db=args.accounts_db or "flux_accounts.db",
        domain=args.domain,
    )


def cmd_keygen(args):
    from flux.identity import FluxIdentity
    identity = FluxIdentity.generate()
    out = {"address": identity.address, "private_key": identity.export_private()}
    if args.save:
        identity.save(args.save)
        print(f"Identity saved to {args.save}")
        print(f"Address: {identity.address}")
    else:
        print(json.dumps(out, indent=2))


def cmd_send(args):
    from flux.identity import FluxIdentity
    from flux.client import FluxClient

    identity = FluxIdentity.from_file(args.identity)
    client = FluxClient(identity, args.server)
    content = args.message or sys.stdin.read()

    async def _send():
        result = await client.send(args.to, content)
        await client.close()
        print(json.dumps(result, indent=2))

    asyncio.run(_send())


def cmd_fetch(args):
    from flux.identity import FluxIdentity
    from flux.client import FluxClient

    identity = FluxIdentity.from_file(args.identity)
    client = FluxClient(identity, args.server)

    async def _fetch():
        msgs = await client.fetch()
        await client.close()
        for msg in msgs:
            print(f"\nfrom: {msg['from']}")
            print(f"  id: {msg['id']}")
            print(f"  at: {msg['t']}")
            print(f"  →  {msg['content']}")
        if not msgs:
            print("No messages.")

    asyncio.run(_fetch())


def cmd_listen(args):
    from flux.identity import FluxIdentity
    from flux.client import FluxClient

    identity = FluxIdentity.from_file(args.identity)
    client = FluxClient(identity, args.server)

    @client.on_message
    async def on_msg(msg):
        print(json.dumps(msg, indent=2) if args.json else f"[{msg['id'][:8]}] {msg['from'][:16]}… → {msg['content']}")

    print(f"Listening as {identity.address}")
    asyncio.run(client.connect_ws())


def cmd_demo(args):
    from flux.identity import FluxIdentity
    from flux.client import FluxClient
    from flux.server import make_app
    from aiohttp import web

    async def _demo():
        alice = FluxIdentity.generate()
        bob = FluxIdentity.generate()

        log.info(f"Alice: {alice.address}")
        log.info(f"Bob:   {bob.address}")

        app = make_app()
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, "localhost", 8765)
        await site.start()
        log.info("FLUX server running on localhost:8765")

        await asyncio.sleep(0.2)

        alice_client = FluxClient(alice, "http://localhost:8765")
        bob_client = FluxClient(bob, "http://localhost:8765")
        received = []

        @bob_client.on_message
        async def on_msg(msg):
            received.append(msg)
            log.info(f"Bob received from {msg['from'][:16]}…: {msg['content']}")

        bob_ws = asyncio.create_task(bob_client.connect_ws())
        alice_ws = asyncio.create_task(alice_client.connect_ws())
        await asyncio.sleep(0.4)

        log.info("--- HTTP send ---")
        r = await alice_client.send(bob.address, "Hey Bob, this is FLUX.")
        log.info(f"Send result: {r}")
        await asyncio.sleep(0.2)

        log.info("--- HTTP fetch ---")
        msgs = await alice_client.fetch()
        log.info(f"Alice inbox: {len(msgs)} messages")

        log.info("--- Status check ---")
        online = await alice_client.status(bob.address)
        log.info(f"Bob online: {online}")

        log.info("--- WebSocket send ---")
        await alice_client.send_ws(bob.address, "Real-time via WebSocket!")
        await asyncio.sleep(0.2)

        log.info(f"Bob received {len(received)} total messages")

        await alice_client.close()
        await bob_client.close()
        bob_ws.cancel()
        alice_ws.cancel()
        await runner.cleanup()
        log.info("Demo complete.")

    asyncio.run(_demo())


def main():
    parser = argparse.ArgumentParser(prog="flux", description="FLUX protocol node and client")
    sub = parser.add_subparsers(dest="command", required=True)

    p_server = sub.add_parser("server", help="Run a FLUX node")
    p_server.add_argument("--host", default="0.0.0.0")
    p_server.add_argument("--port", type=int, default=8765)
    p_server.add_argument("--backend", choices=["memory", "sqlite"], default="memory")
    p_server.add_argument("--db", help="SQLite message store path")
    p_server.add_argument("--accounts-db", help="SQLite accounts store path")
    p_server.add_argument("--domain", help="Domain name for federated addressing, e.g. mail.example.com")
    p_server.add_argument("-d", "--detach", action="store_true", help="Run in background")
    p_server.set_defaults(func=cmd_server)

    p_key = sub.add_parser("keygen", help="Generate a new FLUX identity")
    p_key.add_argument("--save", metavar="FILE", help="Save identity to a JSON file")
    p_key.set_defaults(func=cmd_keygen)

    p_send = sub.add_parser("send", help="Send a message (raw FLUX)")
    p_send.add_argument("--identity", required=True, metavar="FILE")
    p_send.add_argument("--to", required=True)
    p_send.add_argument("--message", "-m")
    p_send.add_argument("--server", default="http://localhost:8765")
    p_send.set_defaults(func=cmd_send)

    p_fetch = sub.add_parser("fetch", help="Fetch pending messages (raw FLUX)")
    p_fetch.add_argument("--identity", required=True, metavar="FILE")
    p_fetch.add_argument("--server", default="http://localhost:8765")
    p_fetch.set_defaults(func=cmd_fetch)

    p_listen = sub.add_parser("listen", help="Listen for messages via WebSocket (raw FLUX)")
    p_listen.add_argument("--identity", required=True, metavar="FILE")
    p_listen.add_argument("--server", default="http://localhost:8765")
    p_listen.add_argument("--json", action="store_true")
    p_listen.set_defaults(func=cmd_listen)

    sub.add_parser("demo", help="Run a self-contained local demo").set_defaults(func=cmd_demo)

    args = parser.parse_args()
    args.func(args)


if __name__ == "__main__":
    main()
