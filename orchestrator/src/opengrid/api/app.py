"""Builds the FastAPI application (02b S7): auth, REST, SSE, OpenAPI docs, and the `opengrid.ui`
mount. `create_app()` is the fixed public entry point (`orchestrator/INTERFACES.md`).
"""

from __future__ import annotations

import asyncio
import contextlib
import logging
from collections.abc import AsyncIterator, Awaitable, Callable
from contextlib import asynccontextmanager
from importlib import import_module

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from psycopg_pool import AsyncConnectionPool

from opengrid.api.csrf import CSRFMiddleware
from opengrid.api.proposals import ProposalStore
from opengrid.api.store import PgStore
from opengrid.api.trace_backend import PgTraceBackend
from opengrid.contracts import AdmissionError
from opengrid.contracts import configure as configure_contracts
from opengrid.contracts.pg_repo import PgContractsRepo
from opengrid.ledger import ReservationError
from opengrid.platform.config import ConfigError, load_config
from opengrid.platform.db import make_pool
from opengrid.platform.heartbeat import write_heartbeat
from opengrid.platform.log import configure_logging
from opengrid.platform.mqtt import SchemaValidationError
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)


@asynccontextmanager
async def _lifespan(app: FastAPI) -> AsyncIterator[None]:
    configure_logging("api")
    cfg = load_config()
    pool = await make_pool(cfg)
    trace_store = TraceStore(PgTraceBackend(pool))
    app.state.config = cfg
    app.state.pool = pool
    app.state.store = PgStore(pool)
    app.state.trace_store = trace_store
    app.state.proposals = ProposalStore()
    # `opengrid.contracts` has no signing key or safety-critical state (unlike guardian/safestop) --
    # its writes are ordinary CRUD/business-rule checks against `og.contract`/`og.opportunity`, so
    # `api` configuring its own repo/trace pair here is the intended reuse (single owner of the
    # write logic, BUILD.md S1 "no duplicated functions"), not a second, independent write path.
    configure_contracts(PgContractsRepo(pool), trace_store)
    try:  # AI copilot: model tier when ai_agent.env provides a key, its no-model tier otherwise
        from opengrid import ai_agent

        ai_agent.configure(cfg)
    except Exception:
        logger.exception("ai_agent.configure failed; copilot routes answer 503 until fixed")
    heartbeat_task = asyncio.create_task(
        heartbeat_loop(pool, interval_s=float(cfg.get("health.heartbeat_interval_s", DEFAULT_HEARTBEAT_S)))
    )
    logger.info("og-api started", extra={"database": cfg.postgres_database})
    try:
        yield
    finally:
        heartbeat_task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await heartbeat_task
        await pool.close()


DEFAULT_HEARTBEAT_S = 5.0
_PROCESS_NAME = "api"  # opengrid.health.model.ALL_PROCESSES


async def heartbeat_loop(
    pool: AsyncConnectionPool,
    *,
    interval_s: float,
    write: Callable[[AsyncConnectionPool, str], Awaitable[None]] = write_heartbeat,
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
) -> None:
    """og-api's process heartbeat (02b S6.4). Without it the health evaluator reported `api` down
    permanently (ALR-PROCESS-DOWN open since the first deploy, live 2026-09-26). A failed write is
    logged and retried on the next beat; it never takes the API down."""
    while True:
        try:
            await write(pool, _PROCESS_NAME)
        except Exception:
            logger.exception("og-api heartbeat write failed")
        await sleep(interval_s)


def _install_exception_handlers(app: FastAPI) -> None:
    @app.exception_handler(NotImplementedError)
    async def _not_implemented(_request: Request, exc: NotImplementedError) -> JSONResponse:
        # A dependency module has not been implemented yet by its owning agent (BUILD.md S4) --
        # surface this plainly (503) rather than a bare 500 traceback; once that module lands, this
        # handler stops firing for it with no change here.
        logger.warning("dependency not implemented", extra={"error": str(exc)})
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": "dependency not yet implemented"},
        )

    @app.exception_handler(RuntimeError)
    async def _not_configured(_request: Request, exc: RuntimeError) -> JSONResponse:
        # `guardian`/`safestop`/`ledger` are singleton modules configured once by their OWNING process
        # (og-guardian/og-safestop/og-engine) -- `api` never wires them directly (K3/K8: their signing
        # keys must not also live in this process). Calling their module-level functions from here is
        # only ever reached on a deployment that hasn't started those processes yet; that is a 503,
        # not a 500.
        logger.warning("dependency not configured in this process", extra={"error": str(exc)})
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": f"dependency not available: {exc}"},
        )

    @app.exception_handler(AdmissionError)
    async def _admission_error(_request: Request, exc: AdmissionError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": {"reason_code": exc.reason_code}}
        )

    @app.exception_handler(ReservationError)
    async def _reservation_error(_request: Request, exc: ReservationError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT, content={"detail": {"reason_code": exc.reason_code}}
        )

    @app.exception_handler(SchemaValidationError)
    async def _schema_invalid(_request: Request, exc: SchemaValidationError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, content={"detail": str(exc)})


def _include_routers(app: FastAPI) -> None:
    from opengrid.api.routers import (
        admin,
        ai,
        billing,
        contracts,
        dispatch,
        dispatch_ledger,
        firmware,
        fleet,
        fleet_bulk,
        fleet_map,
        fleet_search,
        grid_layers,
        health,
        lp_value,
        markets,
        markets_funnel,
        pq,
        profitability,
        profitability_kw,
        retention,
        safestop,
        scenario,
    )

    # Static-path routers go before the routers whose paths take parameters under the same prefix
    # (fleet_search before fleet, lp_value before profitability).
    for router_module in (
        health,
        fleet_search,
        fleet,
        fleet_map,
        fleet_bulk,
        firmware,
        grid_layers,
        markets,
        markets_funnel,
        dispatch,
        dispatch_ledger,
        lp_value,
        profitability,
        pq,
        profitability_kw,
        billing,
        contracts,
        retention,
        safestop,
        admin,
        scenario,
        ai,
    ):
        app.include_router(router_module.router)

    from opengrid.api import views_settlement  # ui-owned read-only settlement view (Profitability, Billing)

    app.include_router(views_settlement.router)

    if customer_api_enabled():
        from opengrid import customer_api

        app.include_router(customer_api.router)
        app.include_router(customer_api.operator_router)


def customer_api_enabled() -> bool:
    """`[api.customer_api].enabled` (default false): the customer API (`opengrid.customer_api`) is only
    mounted when switched on, so it can ship dark. Read defensively like `_mount_ui`'s base path."""
    try:
        return bool(load_config().get("api.customer_api.enabled", False))
    except ConfigError:
        return False


_DEFAULT_UI_BASE_PATH = "/og"


def _mount_ui(app: FastAPI) -> None:
    """Mounts the UI package's router if present (owned by ui-a/ui-b, `orchestrator/INTERFACES.md`
    `opengrid.ui`: `build_router()`). Not required for `api`'s own tests/verification -- absent during
    early parallel development, the API still serves every REST/SSE endpoint on its own.

    Merge fix: this used to call `app.include_router(build_router())` with no `prefix`, so
    `opengrid.ui.routes.control_room`'s `@router.get("/")` (and every other UI screen) was served at the
    FastAPI app's own root `/` instead of `[ui].base_path` (`/og`, `orchestrator/config/orchestrator.toml`,
    per `opengrid.ui.routes`'s own docstring) -- confirmed live: Apache proxies
    `https://base.tocy-net.net/og/` to this app's `/og/`, which 404'd because nothing was ever registered
    there. `[api]`'s REST routers already all use an explicit `/og/api/...` path in each `@router.get`, so
    they were unaffected; only the UI mount was missing its prefix.

    `create_app()` runs before `_lifespan` (which is where `app.state.config` gets set, since it only
    runs once the app actually starts serving), and API unit tests call `create_app()` directly with no
    `OG_CONFIG` set at all (`tests/unit/api/conftest.py`) -- so this loads its own `Config` defensively
    rather than requiring one, falling back to the documented default base path on any `ConfigError`
    (the same value `orchestrator/config/orchestrator.toml`'s `[ui].base_path` ships with).
    """
    try:
        ui_module = import_module("opengrid.ui")
    except ImportError:
        logger.info("opengrid.ui not present yet; API-only mode")
        return
    build_router = getattr(ui_module, "build_router", None)
    if build_router is None:
        logger.warning("opengrid.ui has no build_router(); skipping UI mount")
        return

    try:
        base_path = str(load_config().get("ui.base_path", _DEFAULT_UI_BASE_PATH))
    except ConfigError:
        base_path = _DEFAULT_UI_BASE_PATH

    try:
        app.include_router(build_router(), prefix=base_path)
    except NotImplementedError:
        logger.info("opengrid.ui.build_router() not implemented yet; API-only mode")


def _install_csrf_protection(app: FastAPI) -> None:
    """Wires `opengrid.api.csrf.CSRFMiddleware` (qa/security-review.md F-03) over every route this
    process serves. `[api].csrf_allowed_hosts` lets the production host join the always-allowed loopback
    set without a code change; defensively defaults to the deployed host if config can't load yet (same
    tolerance pattern as `_mount_ui`'s base_path resolution, for the same reason -- API unit tests call
    `create_app()` with no `OG_CONFIG` set)."""
    try:
        cfg = load_config()
        configured = cfg.get("api.csrf_allowed_hosts", ["base.tocy-net.net"])
        cookie_secure = bool(cfg.get("api.csrf_cookie_secure", True))
    except ConfigError:
        configured = ["base.tocy-net.net"]
        cookie_secure = False  # no OG_CONFIG -> unit tests running over TestClient's plain-http default
    allowed_hosts = frozenset(str(h) for h in configured)
    app.add_middleware(CSRFMiddleware, allowed_hosts=allowed_hosts, cookie_secure=cookie_secure)


def create_app() -> FastAPI:
    """Build the FastAPI application: mounts `opengrid.ui` routes, the REST endpoints of 02b S7.1, and
    the SSE streams of 02b S7.2. Binds to `[api].bind_host`/`bind_port` (loopback only, S9.3)."""
    app = FastAPI(
        title="OpenGrid Orchestrator API",
        docs_url="/og/api/docs",
        openapi_url="/og/api/openapi.json",
        redoc_url=None,
        lifespan=_lifespan,
    )
    _install_exception_handlers(app)
    _include_routers(app)
    _mount_ui(app)
    _install_csrf_protection(app)

    @app.exception_handler(LookupError)
    async def _not_found(_request: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    return app
