import asyncio
import hashlib
import hmac
import json
import logging
from typing import Optional

import aiohttp

from .constants import MESH_HEADER, MESH_CONFIG_PATH

log = logging.getLogger("flux.mesh")


def load_mesh_config(path=MESH_CONFIG_PATH) -> dict:
    from pathlib import Path
    p = Path(path)
    if not p.exists():
        return {}
    try:
        data = json.loads(p.read_text())
        meshes = data.get("meshes", {})
        log.info(f"loaded {len(meshes)} mesh config(s): {list(meshes.keys())}")
        return meshes
    except Exception as e:
        log.error(f"failed to load mesh config: {e}")
        return {}


def _derive(token: str) -> str:
    return hashlib.sha256(f"flux-mesh:{token}".encode()).hexdigest()


def validate_mesh_token(raw: str, meshes: dict) -> Optional[str]:
    d = _derive(raw)
    for name, cfg in meshes.items():
        if hmac.compare_digest(d, _derive(cfg["token"])):
            return name
    return None


class MeshRelay:

    def __init__(self, meshes: dict, local_url: str):
        self._meshes = meshes
        self._local = local_url.rstrip("/")

    async def relay(self, msg: dict, source_mesh: Optional[str] = None) -> dict[str, str]:
        results: dict[str, str] = {}
        names = [source_mesh] if source_mesh else list(self._meshes.keys())

        async with aiohttp.ClientSession() as session:
            tasks = []
            for name in names:
                cfg = self._meshes.get(name)
                if not cfg:
                    continue
                mode = cfg.get("mode", "broadcast")
                peers = [p.rstrip("/") for p in cfg.get("peers", []) if p.rstrip("/") != self._local]

                if mode == "broadcast":
                    for peer in peers:
                        tasks.append(self._send(session, peer, msg, cfg["token"], results))
                elif mode == "chain":
                    tasks.append(self._chain(session, peers, msg, cfg["token"], results))
                elif mode == "hybrid":
                    tasks.append(self._hybrid(session, peers, msg, cfg["token"], results))

            await asyncio.gather(*tasks, return_exceptions=True)

        return results

    async def _send(self, session, peer, msg, token, results):
        try:
            async with session.post(
                f"{peer}/mesh/relay", json=msg,
                headers={MESH_HEADER: _derive(token)},
                timeout=aiohttp.ClientTimeout(total=5),
            ) as r:
                results[peer] = "ok" if r.status == 200 else f"http_{r.status}"
                log.info(f"mesh → {peer}: {results[peer]}")
        except Exception as e:
            results[peer] = "failed"
            log.warning(f"mesh → {peer}: {e}")

    async def _chain(self, session, peers, msg, token, results):
        for peer in peers:
            await self._send(session, peer, msg, token, results)
            if results.get(peer) == "ok":
                for p in peers:
                    if p not in results:
                        results[p] = "skipped"
                return

    async def _hybrid(self, session, peers, msg, token, results):
        recipient = msg.get("to", "")
        matched = None
        for peer in peers:
            try:
                async with session.get(f"{peer}/status/{recipient}", timeout=aiohttp.ClientTimeout(total=2)) as r:
                    if r.status == 200:
                        d = await r.json()
                        if d.get("online"):
                            matched = peer
                            break
            except Exception:
                pass

        if matched:
            await self._send(session, matched, msg, token, results)
            for p in peers:
                if p not in results:
                    results[p] = "skipped"
        else:
            await asyncio.gather(*[self._send(session, p, msg, token, results) for p in peers])


def make_mesh_routes(store, presence, meshes: dict):
    from aiohttp import web
    from .message import validate_fields, check_freshness, verify_message, append_route, strip_bcc
    from .integrity import append_integrity_hop

    async def route_relay(request: web.Request) -> web.Response:
        token_header = request.headers.get(MESH_HEADER, "")
        if not token_header:
            return web.json_response({"ok": False, "error": "missing mesh token"}, status=401)

        matched = None
        for name, cfg in meshes.items():
            if hmac.compare_digest(token_header, _derive(cfg["token"])):
                matched = name
                break

        if not matched:
            return web.json_response({"ok": False, "error": "invalid mesh token"}, status=403)

        try:
            msg = await request.json()
        except Exception:
            return web.json_response({"ok": False, "error": "invalid json"}, status=400)

        if not validate_fields(msg):
            return web.json_response({"ok": False, "error": "missing fields"}, status=400)
        if not check_freshness(msg):
            return web.json_response({"ok": False, "error": "message too old"}, status=400)
        if not verify_message(msg):
            return web.json_response({"ok": False, "error": "invalid signature"}, status=403)

        domain = request.app.get("domain", request.host)
        msg = append_route(msg, domain)
        msg = append_integrity_hop(msg, domain)
        msg = strip_bcc(msg)

        if await presence.deliver(msg["to"], {"type": "msg", "msg": msg}):
            delivery = "realtime"
        else:
            queued = await store.enqueue(msg)
            delivery = "queued" if queued else "dropped"

        log.info(f"mesh [{matched}] recv {msg['id'][:8]}… → {delivery}")
        return web.json_response({"ok": True, "delivery": delivery, "mesh": matched})

    async def route_info(request: web.Request) -> web.Response:
        return web.json_response({"ok": True, "meshes": list(meshes.keys()), "count": len(meshes)})

    return [web.post("/mesh/relay", route_relay), web.get("/mesh/info", route_info)]