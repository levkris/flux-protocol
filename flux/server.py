import logging
import os

from aiohttp import web

from .accounts import AccountStore
from .constants import DEFAULT_HOST, DEFAULT_PORT
from .presence import PresenceRegistry
from .routes import make_routes
from .store import BaseStore, create_store
from .ws import make_ws_handler

log = logging.getLogger("flux.server")


def make_app(
    store: BaseStore | None = None,
    presence: PresenceRegistry | None = None,
    domain: str | None = None,
    accounts: AccountStore | None = None,
) -> web.Application:
    """
    Build and return the aiohttp Application.

    Pass domain + accounts to enable the federated account system.
    Without them the server operates as a raw FLUX relay only.
    """
    if store is None:
        store = create_store("memory")
    if presence is None:
        presence = PresenceRegistry()

    app = web.Application()
    app["store"] = store
    app["presence"] = presence

    app.router.add_routes(make_routes(store, presence))
    app.router.add_get("/ws", make_ws_handler(store, presence))

    if domain and accounts:
        from .account_routes import make_account_routes
        app["accounts"] = accounts
        app["domain"] = domain
        app.router.add_routes(make_account_routes(domain))
        log.info(f"federated account system enabled for domain: {domain}")

    return app


def run_server(
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    backend: str = "memory",
    db_path: str = "flux.db",
    accounts_db: str = "flux_accounts.db",
    domain: str | None = None,
):
    """Start the FLUX server. This is the main production entrypoint."""
    store = create_store(backend, db_path=db_path)
    presence = PresenceRegistry()

    accounts = None
    if domain:
        accounts = AccountStore(db_path=accounts_db)

    app = make_app(store, presence, domain=domain, accounts=accounts)

    log.info(f"FLUX server starting on {host}:{port} (store={backend})")
    if domain:
        log.info(f"domain: {domain}")
    web.run_app(app, host=host, port=port, access_log=None)
