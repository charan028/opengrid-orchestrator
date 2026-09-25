"""Builds the FastAPI application (02b S7): auth, REST, SSE, OpenAPI docs, and the `opengrid.ui`
mount. `create_app()` is the fixed public entry point (`orchestrator/INTERFACES.md`).
"""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from importlib import import_module

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from opengrid.api.proposals import ProposalStore
from opengrid.api.store import PgStore
from opengrid.api.trace_backend import PgTraceBackend
from opengrid.contracts import AdmissionError
from opengrid.contracts import configure as configure_contracts
from opengrid.contracts.pg_repo import PgContractsRepo
from opengrid.ledger import ReservationError
from opengrid.platform.config import load_config
from opengrid.platform.db import make_pool
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
    logger.info("og-api started", extra={"database": cfg.postgres_database})
    try:
        yield
    finally:
        await pool.close()


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
        billing,
        contracts,
        dispatch,
        fleet,
        health,
        markets,
        profitability,
        retention,
        safestop,
        scenario,
    )

    for router_module in (
        health,
        fleet,
        markets,
        dispatch,
        profitability,
        billing,
        contracts,
        retention,
        safestop,
        scenario,
    ):
        app.include_router(router_module.router)


def _mount_ui(app: FastAPI) -> None:
    """Mounts the UI package's router if present (owned by ui-a/ui-b, `orchestrator/INTERFACES.md`
    `opengrid.ui`: `build_router()`). Not required for `api`'s own tests/verification -- absent during
    early parallel development, the API still serves every REST/SSE endpoint on its own."""
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
        app.include_router(build_router())
    except NotImplementedError:
        logger.info("opengrid.ui.build_router() not implemented yet; API-only mode")


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

    @app.exception_handler(LookupError)
    async def _not_found(_request: Request, exc: LookupError) -> JSONResponse:
        return JSONResponse(status_code=status.HTTP_404_NOT_FOUND, content={"detail": str(exc)})

    return app
