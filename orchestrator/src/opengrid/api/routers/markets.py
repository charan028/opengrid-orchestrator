"""Markets & feeds screen (02b S7.1, S8 screen 4): feed observation windows and forecast quantiles."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Query

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.deps import get_store
from opengrid.api.store import StoreProtocol

router = APIRouter(prefix="/og/api", tags=["markets"])


@router.get("/markets/series")
async def market_series(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    product: str | None = None,
    series_key: str | None = None,
    from_: Annotated[datetime | None, Query(alias="from")] = None,
    to: datetime | None = None,
) -> list[dict[str, Any]]:
    t1 = to or datetime.now(UTC)
    t0 = from_ or (t1 - timedelta(hours=24))
    rows = await store.feed_series(product=product, series=series_key, t0=t0, t1=t1)
    return [r.model_dump(mode="json") for r in rows]


@router.get("/markets/feed-status")
async def feed_status(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> list[dict[str, Any]]:
    """Freshness/source-status table (02b S8 screen 4), backed by `feed_status` (02b S2.7)."""
    statuses = await store.feed_statuses()
    now = datetime.now(UTC)
    return [
        {
            "source": s.source,
            "product": s.product,
            "last_value_at": s.last_value_at.isoformat() if s.last_value_at else None,
            "age_s": (now - s.last_value_at).total_seconds() if s.last_value_at else None,
            "breaker_open": s.breaker_open,
            "consecutive_failures": s.consecutive_failures,
        }
        for s in statuses
    ]


@router.get("/forecast")
async def forecast(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    series_key: str,
    kind: str = "price",
) -> dict[str, Any]:
    """P10/P50/P90 quantile band, 96 steps (02b S3, S7.1), as parallel arrays --
    `opengrid.ui.routes.markets.forecast_band_view` reads `forecast["p10"/"p50"/"p90"/"ts"]` directly."""
    points = await store.forecast_series(series_key=series_key, kind=kind)
    return {
        "series_key": series_key,
        "kind": kind,
        "ts": [p.interval_start_utc.isoformat() for p in points],
        "p10": [p.p10 for p in points],
        "p50": [p.p50 for p in points],
        "p90": [p.p90 for p in points],
        "firm_fitness": [p.firm_fitness for p in points],
    }
