"""FastAPI app factory for ogsim.market."""

from __future__ import annotations

from fastapi import FastAPI

from ogsim.market.config import MarketConfig
from ogsim.market.routes_admin import router as admin_router
from ogsim.market.routes_eia import router as eia_router
from ogsim.market.routes_ercot import router as ercot_router
from ogsim.market.routes_ercot import token_router as ercot_token_router
from ogsim.market.routes_nws import router as nws_router
from ogsim.market.runtime import MarketRuntime


def create_app(cfg: MarketConfig | None = None) -> FastAPI:
    app = FastAPI(
        title="ogsim.market", description="Simulated ERCOT/EIA/NWS APIs for OpenGrid integration testing"
    )
    app.state.runtime = MarketRuntime(cfg)
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
