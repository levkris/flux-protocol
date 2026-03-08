import logging
from aiohttp import web
from .accounts import AccountStore
from .constants import DEFAULT_HOST, DEFAULT_PORT
from .presence import PresenceRegistry
from .routes import make_routes
from .store import BaseStore, create_store
from .ws import make_ws_handler

log = logging.getLogger("flux.server")


def make_app(store=None, presence=None, domain=None, accounts=None,
             mesh_config_path="mesh.config.json", local_url=""):
    if store is None:
        store = create_store("memory")
    if presence is None:
        presence = PresenceRegistry()

    from .mesh import load_mesh_config, MeshRelay, make_mesh_routes
    meshes = load_mesh_config(mesh_config_path)
    mesh_relay = MeshRelay(meshes, local_url) if meshes else None

    app = web.Application()
    app["store"] = store
    app["presence"] = presence
    app["domain"] = domain or ""
    app["meshes"] = meshes

    app.router.add_routes(make_routes(store, presence, domain=domain or "", mesh_relay=mesh_relay))
    app.router.add_get("/ws", make_ws_handler(store, presence, domain=domain or "", mesh_relay=mesh_relay))

    if meshes:
        app.router.add_routes(make_mesh_routes(store, presence, meshes))
        log.info(f"mesh enabled: {list(meshes.keys())}")

    if domain and accounts:
        from .account_routes import make_account_routes
        app["accounts"] = accounts
        app.router.add_routes(make_account_routes(domain))
        log.info(f"federation enabled for domain: {domain}")

    return app


def run_server(host=DEFAULT_HOST, port=DEFAULT_PORT, backend="memory",
               db_path="flux.db", accounts_db="flux_accounts.db",
               domain=None, mesh_config_path="mesh.config.json"):
    store = create_store(backend, db_path=db_path)
    presence = PresenceRegistry()
    accounts = AccountStore(db_path=accounts_db) if domain else None
    local_url = f"http://{host}:{port}"

    app = make_app(store, presence, domain=domain, accounts=accounts,
                   mesh_config_path=mesh_config_path, local_url=local_url)

    log.info(f"FLUX server starting on {host}:{port} (store={backend})")
    if domain:
        log.info(f"domain: {domain}")
    web.run_app(app, host=host, port=port, access_log=None)