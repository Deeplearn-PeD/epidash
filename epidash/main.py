"""EpiDash application factory.

Wires together:
- the SQLite store (schema applied at startup, bootstrap key seeded);
- the public REST API (published dashboards + tweak flow);
- the MCP server mounted at settings.mcp_path (dk_-key authenticated);
- the static viewer UI served at /.
"""

from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import JSONResponse
from starlette.datastructures import MutableHeaders
from starlette.routing import Mount as StarletteMount, Route as StarletteRoute
from starlette.staticfiles import StaticFiles

from epidash.api import routes_public
from epidash.config import get_settings
from epidash.logging import get_logger
from epidash.mcpsrv import audit as mcp_audit
from epidash.mcpsrv import server as mcp_server
from epidash.mcpsrv.auth import McpAuthMiddleware, set_store as auth_set_store
from epidash import refresher
from epidash.store import Store

logger = get_logger(__name__)

_STATIC_DIR = Path(__file__).parent / "static"

# Path prefixes handled by non-static routers (excluded from the
# no-cache middleware below).
_DYNAMIC_PREFIXES = ("/api/", "/mcp", "/healthz")


class NoCacheStaticMiddleware:
    """Force browsers to revalidate static assets on every load.

    Without a Cache-Control header, browsers heuristic-cache app.js /
    style.css for a long time and viewers keep seeing a stale UI after
    deploys. `no-cache` still allows cheap 304 responses via ETag.
    """

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope["path"].startswith(_DYNAMIC_PREFIXES):
            await self.app(scope, receive, send)
            return

        async def send_with_cache_control(message):
            if message["type"] == "http.response.start":
                headers = MutableHeaders(scope=message)
                headers["Cache-Control"] = "no-cache"
            await send(message)

        await self.app(scope, receive, send_with_cache_control)


def seed_bootstrap_key(store: Store, raw_key: str) -> None:
    """Idempotently register the env-provided bootstrap key, if any."""
    if not raw_key:
        return
    if store.verify_key(raw_key) is None:
        store.add_key(raw_key, name="bootstrap")
        logger.info("Seeded bootstrap control key (%s...)", raw_key[:8])


def add_mcp_routes(routes: list, store: Store) -> None:
    """Register the MCP endpoint, mirroring the EpidBot mounting trick:
    a plain Route serves the bare path (MCP clients don't follow 307
    redirects) and a Mount serves the trailing-slash variants. The sub-app
    receives the original, unrewritten path so the session manager's path
    check stays consistent."""
    settings = get_settings()
    prefix = settings.mcp_path.rstrip("/")
    wrapped = McpAuthMiddleware(mcp_server.mcp.streamable_http_app(), store=store)
    routes.append(StarletteRoute(prefix, wrapped))
    routes.append(StarletteMount(prefix + "/", wrapped))


def create_app(store: Store | None = None, *, run_mcp_lifespan: bool = True) -> FastAPI:
    settings = get_settings()
    store = store or Store(settings.db_path)

    mcp_server.set_store(store)
    auth_set_store(store)
    mcp_audit.set_store(store)
    routes_public.set_store(store)
    seed_bootstrap_key(store, settings.bootstrap_key)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        # Mounted ASGI sub-apps don't get their lifespan run by Starlette,
        # so the MCP session manager is run here (same as EpidBot).
        # run_mcp_lifespan=False is for tests: the StreamableHTTP session
        # manager may only run once per process, and the MCP tool surface
        # is covered via the in-memory transport instead.
        session_manager = mcp_server.mcp.session_manager
        if run_mcp_lifespan:
            async with session_manager.run():
                refresher.start(store)
                try:
                    yield
                finally:
                    await refresher.stop()
        else:
            yield
        logger.info(
            "EpiDash ready (db=%s, mcp=%s, epidbot_tweaks=%s)",
            settings.db_path,
            settings.mcp_path,
            "on" if settings.epidbot_configured else "off",
        )

    app = FastAPI(title="EpiDash", version="0.1.0", lifespan=lifespan)

    # Outermost: static assets must always be revalidated so UI changes
    # land for viewers as soon as the container is redeployed.
    app.add_middleware(NoCacheStaticMiddleware)

    add_mcp_routes(app.router.routes, store)

    @app.get("/healthz", include_in_schema=False)
    async def healthz() -> JSONResponse:
        return JSONResponse({"status": "ok"})

    app.include_router(routes_public.router)

    if _STATIC_DIR.is_dir():
        app.mount("/", StaticFiles(directory=str(_STATIC_DIR), html=True), name="static")

    return app


app = create_app()
