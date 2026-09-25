"""FastAPI app factory for ogsim.market."""

from __future__ import annotations

from collections.abc import Callable
from datetime import datetime

from fastapi import FastAPI

from ogsim.market.config import MarketConfig
from ogsim.market.routes_admin import router as admin_router
from ogsim.market.routes_eia import router as eia_router
from ogsim.market.routes_ercot import router as ercot_router
from ogsim.market.routes_ercot import token_router as ercot_token_router
from ogsim.market.routes_nws import router as nws_router
from ogsim.market.runtime import MarketRuntime


def create_app(cfg: MarketConfig | None = None, clock: Callable[[], datetime] | None = None) -> FastAPI:
    """Builds the ogsim.market FastAPI app. `clock`, if given, replaces
    `MarketRuntime`'s default `datetime.now(UTC)` so tests can inject a
    fake clock and drive anomaly start/expiry deterministically instead of
    sleeping real time."""
    app = FastAPI(
        title="ogsim.market", description="Simulated ERCOT/EIA/NWS APIs for OpenGrid integration testing"
    )
    app.state.runtime = MarketRuntime(cfg, clock=clock)
    app.include_router(ercot_token_router, tags=["ercot"])
    app.include_router(ercot_router, tags=["ercot"])
    app.include_router(eia_router, tags=["eia"])
    app.include_router(nws_router, tags=["nws"])
    app.include_router(admin_router, tags=["admin"])

    @app.get("/healthz")
    async def healthz() -> dict[str, bool]:
        return {"ok": True}

    return app


app = create_app()
