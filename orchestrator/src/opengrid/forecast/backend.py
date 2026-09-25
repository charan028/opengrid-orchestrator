"""I/O contracts for `forecast`: history input and persistence output, kept as `Protocol`s so the
quantile logic in `quantiles.py`/`service.py` has no direct DB import (BUILD.md S5a "pure logic
separated from I/O", mirroring `opengrid.trace.store`'s `TraceBackend` pattern).

`HistoryProvider` is satisfied structurally by the `opengrid.feeds` module itself (it exposes
`latest`/`window` with exactly this shape) -- production wiring passes that module in directly rather
than re-querying `feed_obs`, since `feeds` owns that table (BUILD.md S1 "no duplicated functions").
Tests pass a fake.
"""

from __future__ import annotations

from datetime import datetime
from typing import Protocol

from opengrid.core.models.platform import FeedObs
from opengrid.forecast.models import ForecastRow


class HistoryProvider(Protocol):
    """Structural match for `opengrid.feeds`'s public interface (02b S2)."""

    async def latest(self, series: str) -> FeedObs: ...

    async def window(self, series: str, t0: datetime, t1: datetime) -> list[FeedObs]: ...


class ForecastBackend(Protocol):
    """Storage contract for the `og.forecast` table (02b S3), owned solely by `forecast`."""

    async def upsert_rows(self, rows: list[ForecastRow]) -> None:
        """Idempotent upsert keyed on (series_key, kind, interval_start_utc) -- a recompute
        overwrites the prior quantiles for that slot in place, per 02b S3 "recomputed every 15
        minutes... and on demand"."""
        ...

    async def fetch_range(self, horizon_start: datetime, horizon_end: datetime) -> list[ForecastRow]:
        """Return persisted rows with `horizon_start <= interval_start_utc < horizon_end`, any
        series/kind, ordered by (series_key, kind, interval_start_utc)."""
        ...
