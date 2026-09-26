"""Backfill ERCOT price/load history into `og.feed_obs` so the forecast's 14-day same-slot pools fill
(the cold start left every slot NOT_FOR_FIRM).

Products (the live feed's own ERCOT client, auth/key rotation and normalizers -- `opengrid.feeds`):

- `np6-905-cd` settlement point prices, load zones and hubs only (`settlementPointType` LZ, HU, AH;
  rows kept only for `LZ_*`/`HB_*` settlement points -- never every resource node).
- `np6-345-cd` actual system load by weather zone.

Writes are insert-only on the live ingest's natural key `(source, product, series, ts)` (`ON CONFLICT
DO NOTHING`): a row the live poll already wrote is never touched, and re-running is harmless. The
forecast reads `og.feed_obs` on every cycle, so it picks the history up on its next run, no restart.

Rate: at most 6 data requests/min (hard cap) -- production og-feeds already spends 24 of ERCOT's ~30/min
account budget. Any HTTP error that survives the shared client's retries (including a persistent 429)
stops the run with the checkpoint saved; re-run to resume from the next unfetched page.

Usage (secrets come from the environment only -- run it under systemd-run with
`EnvironmentFile=/etc/opengrid/api_keys.env`; nothing here reads or prints secret files):

    python tools/ercot_backfill.py [--start YYYY-MM-DD --end YYYY-MM-DD | --days 14] [--dry-run]
    python tools/ercot_backfill.py --status
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, Protocol

import httpx

from opengrid.core.models.platform import FeedObs
from opengrid.core.timeutil import to_market_tz
from opengrid.feeds.ercot import ErcotPage, ercot_client_from_config
from opengrid.feeds.http_client import FeedHttpError
from opengrid.feeds.normalize import SOURCE_ERCOT, FeedDataError
from opengrid.feeds.store import FeedStore
from opengrid.feeds.token_bucket import TokenBucket
from opengrid.platform.config import load_config
from opengrid.platform.db import make_pool
from opengrid.platform.log import configure_logging

logger = logging.getLogger("opengrid.tools.ercot_backfill")

SPP_PRODUCT = "np6-905-cd"
LOAD_PRODUCT = "np6-345-cd"
PRODUCTS: tuple[str, ...] = (SPP_PRODUCT, LOAD_PRODUCT)
#: ERCOT settlement point types for load zones (LZ), hubs (HU) and hub averages (AH, HB_HUBAVG/HB_BUSAVG).
SPP_POINT_TYPES: tuple[str, ...] = ("LZ", "HU", "AH")
SPP_SERIES_PREFIXES: tuple[str, ...] = ("LZ_", "HB_")

MAX_RATE_PER_MIN = 6.0
DEFAULT_DAYS = 14
DEFAULT_PAGE_SIZE = 5000
DEFAULT_CHECKPOINT = Path("/var/lib/opengrid/ercot_backfill/checkpoint.json")

EXIT_OK = 0
EXIT_ABORTED = 3


@dataclass(frozen=True, slots=True)
class Unit:
    """One (product, settlement-point type, operating day) request series -- the checkpoint granule."""

    product: str
    point_type: str | None
    day: date

    @property
    def key(self) -> str:
        return f"{self.product}|{self.point_type or '-'}|{self.day.isoformat()}"

    def params(self, page: int, page_size: int) -> dict[str, str]:
        d = self.day.isoformat()
        paging = {"page": str(page), "size": str(page_size)}
        if self.product == SPP_PRODUCT:
            if self.point_type is None:
                raise ValueError("np6-905-cd units need a settlement point type")
            return {
                "deliveryDateFrom": d,
                "deliveryDateTo": d,
                "settlementPointType": self.point_type,
                **paging,
            }
        if self.product == LOAD_PRODUCT:
            return {"operatingDayFrom": d, "operatingDayTo": d, **paging}
        raise ValueError(f"backfill does not support product {self.product!r}")


def plan_units(start: date, end: date, products: Sequence[str] = PRODUCTS) -> list[Unit]:
    """Every unit for `[start, end]` (inclusive), newest day first: the most recent days matter most to
    the forecast's lookback, so an interrupted run has already loaded the useful part."""
    if end < start:
        raise ValueError(f"end {end} is before start {start}")
    units: list[Unit] = []
    day = end
    while day >= start:
        for product in products:
            if product == SPP_PRODUCT:
                units.extend(Unit(product, t, day) for t in SPP_POINT_TYPES)
            else:
                units.append(Unit(product, None, day))
        day -= timedelta(days=1)
    return units


def keep_obs(unit: Unit, obs: list[FeedObs]) -> list[FeedObs]:
    if unit.product == SPP_PRODUCT:
        return [o for o in obs if o.series.startswith(SPP_SERIES_PREFIXES)]
    return obs


@dataclass
class Checkpoint:
    """Resumable progress, persisted as JSON after every page (atomic replace)."""

    path: Path
    start: str = ""
    end: str = ""
    products: list[str] = field(default_factory=list)
    done: dict[str, dict[str, int]] = field(default_factory=dict)
    next_page: dict[str, int] = field(default_factory=dict)
    partial: dict[str, dict[str, int]] = field(default_factory=dict)
    requests: int = 0
    last_error: str | None = None
    updated_at: str = ""

    @classmethod
    def load(cls, path: Path) -> Checkpoint:
        if not path.exists():
            return cls(path=path)
        raw = json.loads(path.read_text(encoding="utf-8"))
        return cls(
            path=path,
            start=raw.get("start", ""),
            end=raw.get("end", ""),
            products=list(raw.get("products", [])),
            done=dict(raw.get("done", {})),
            next_page=dict(raw.get("next_page", {})),
            partial=dict(raw.get("partial", {})),
            requests=int(raw.get("requests", 0)),
            last_error=raw.get("last_error"),
            updated_at=raw.get("updated_at", ""),
        )

    def save(self) -> None:
        self.updated_at = datetime.now(UTC).isoformat()
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(self.path.suffix + ".tmp")
        body = {k: v for k, v in self.__dict__.items() if k != "path"}
        tmp.write_text(json.dumps(body, indent=1, sort_keys=True), encoding="utf-8")
        os.replace(tmp, self.path)


def status_report(cp: Checkpoint) -> dict[str, Any]:
    """Progress per product: units and whole days done (a price day needs all its point types)."""
    if not cp.start:
        return {"state": "not started", "checkpoint": str(cp.path)}
    units = plan_units(date.fromisoformat(cp.start), date.fromisoformat(cp.end), cp.products)
    per_product: dict[str, dict[str, Any]] = {}
    for product in cp.products:
        mine = [u for u in units if u.product == product]
        days = sorted({u.day for u in mine})
        days_done = [d for d in days if all(u.key in cp.done for u in mine if u.day == d)]
        per_product[product] = {
            "units_done": sum(u.key in cp.done for u in mine),
            "units_total": len(mine),
            "days_loaded": len(days_done),
            "days_total": len(days),
            "rows_fetched": sum(cp.done[u.key]["rows"] for u in mine if u.key in cp.done),
            "rows_inserted": sum(cp.done[u.key]["inserted"] for u in mine if u.key in cp.done),
        }
    complete = all(p["units_done"] == p["units_total"] for p in per_product.values())
    return {
        "state": "complete" if complete else "in progress",
        "range": [cp.start, cp.end],
        "requests": cp.requests,
        "last_error": cp.last_error,
        "updated_at": cp.updated_at,
        "products": per_product,
    }


class PageFetcher(Protocol):
    async def fetch_page(
        self, product: str, *, params: dict[str, str], now: datetime | None = None
    ) -> ErcotPage: ...


class ObsSink(Protocol):
    async def insert_obs_if_absent(self, rows: list[FeedObs]) -> int: ...


class BackfillAbortedError(RuntimeError):
    """An ERCOT/data error stopped the run; the checkpoint holds everything completed so far."""


async def run_backfill(
    units: Sequence[Unit],
    fetcher: PageFetcher,
    sink: ObsSink,
    checkpoint: Checkpoint,
    *,
    acquire: Callable[[], Awaitable[None]],
    page_size: int = DEFAULT_PAGE_SIZE,
) -> Checkpoint:
    """Fetch every unit not yet in `checkpoint.done`, page by page (resuming at `next_page`), each page
    gated by `acquire()` (the rate limiter), inserting as it goes and saving the checkpoint after every
    page. Raises `BackfillAbortedError` on the first unrecoverable fetch/normalize error."""
    for unit in units:
        if unit.key in checkpoint.done:
            continue
        page = checkpoint.next_page.get(unit.key, 1)
        totals = checkpoint.partial.get(unit.key, {"rows": 0, "inserted": 0})
        while True:
            await acquire()
            checkpoint.requests += 1
            try:
                result = await fetcher.fetch_page(unit.product, params=unit.params(page, page_size))
            except (FeedHttpError, FeedDataError) as exc:
                status = getattr(exc, "status_code", None)
                checkpoint.last_error = f"{unit.key} page {page}: {type(exc).__name__} status={status}"
                checkpoint.save()
                logger.error("backfill stopped", extra={"unit": unit.key, "page": page, "status": status})
                raise BackfillAbortedError(checkpoint.last_error) from None
            rows = keep_obs(unit, result.observations)
            inserted = await sink.insert_obs_if_absent(rows)
            totals = {"rows": totals["rows"] + len(rows), "inserted": totals["inserted"] + inserted}
            logger.info(
                "backfill page",
                extra={
                    "unit": unit.key,
                    "page": page,
                    "pages": result.total_pages,
                    "rows": len(rows),
                    "inserted": inserted,
                },
            )
            if page >= result.total_pages:
                break
            page += 1
            checkpoint.next_page[unit.key] = page
            checkpoint.partial[unit.key] = totals
            checkpoint.save()
        checkpoint.done[unit.key] = totals
        checkpoint.next_page.pop(unit.key, None)
        checkpoint.partial.pop(unit.key, None)
        checkpoint.last_error = None
        checkpoint.save()
    return checkpoint


def _parse_args(argv: list[str]) -> argparse.Namespace:
    p = argparse.ArgumentParser(prog="tools/ercot_backfill.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--config", default=None, help="orchestrator.toml (defaults to OG_CONFIG)")
    p.add_argument("--start", type=date.fromisoformat, help="first operating day (CT), inclusive")
    p.add_argument("--end", type=date.fromisoformat, help="last operating day (CT), inclusive")
    p.add_argument(
        "--days", type=int, default=DEFAULT_DAYS, help="days before the earliest stored observation"
    )
    p.add_argument("--rate-per-min", type=float, default=MAX_RATE_PER_MIN, help="data requests/min (max 6)")
    p.add_argument("--page-size", type=int, default=DEFAULT_PAGE_SIZE)
    p.add_argument("--checkpoint", type=Path, default=DEFAULT_CHECKPOINT)
    p.add_argument("--dry-run", action="store_true", help="print the plan; no API calls, no writes")
    p.add_argument("--status", action="store_true", help="print progress from the checkpoint and exit")
    args = p.parse_args(argv)
    if not 0 < args.rate_per_min <= MAX_RATE_PER_MIN:
        p.error(f"--rate-per-min must be in (0, {MAX_RATE_PER_MIN:g}] (production og-feeds uses 24/min)")
    if (args.start is None) != (args.end is None):
        p.error("--start and --end go together")
    return args


async def _default_range(store: FeedStore, days: int) -> tuple[date, date]:
    """`[earliest - days, earliest]` in CT operating days, from the older of the two products' earliest
    stored ERCOT observation (today when there is none). The earliest day itself is included: it is
    usually only partly covered, and existing rows are never overwritten."""
    earliest = [await store.earliest_ts(source=SOURCE_ERCOT, product=p) for p in PRODUCTS]
    known = [to_market_tz(ts).date() for ts in earliest if ts is not None]
    end = min(known) if known else to_market_tz(datetime.now(UTC)).date()
    return end - timedelta(days=days), end


async def _async_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if args.status:
        print(json.dumps(status_report(Checkpoint.load(args.checkpoint)), indent=1))
        return EXIT_OK

    cfg = load_config(args.config)
    pool = await make_pool(cfg)
    try:
        store = FeedStore(pool)
        checkpoint = Checkpoint.load(args.checkpoint)
        if args.start is not None:
            start, end = args.start, args.end
        elif checkpoint.start:
            start, end = date.fromisoformat(checkpoint.start), date.fromisoformat(checkpoint.end)
        else:
            start, end = await _default_range(store, args.days)
        if checkpoint.start and (checkpoint.start, checkpoint.end) != (start.isoformat(), end.isoformat()):
            logger.error("checkpoint is for a different range; use another --checkpoint")
            return EXIT_ABORTED
        units = plan_units(start, end)
        pending = [u for u in units if u.key not in checkpoint.done]
        plan = {
            "range": [start.isoformat(), end.isoformat()],
            "units_total": len(units),
            "units_pending": len(pending),
            "min_requests": len(pending),
            "est_minutes": round(len(pending) / args.rate_per_min, 1),
            "rate_per_min": args.rate_per_min,
        }
        print(json.dumps(plan))
        if args.dry_run:
            return EXIT_OK

        checkpoint.start, checkpoint.end, checkpoint.products = (
            start.isoformat(),
            end.isoformat(),
            list(PRODUCTS),
        )
        checkpoint.save()
        bucket = TokenBucket(capacity=1.0, refill_per_s=args.rate_per_min / 60.0)
        async with httpx.AsyncClient(follow_redirects=True) as http_client:
            client = ercot_client_from_config(cfg.get("feeds.ercot", {}), http_client)
            try:
                await run_backfill(
                    pending, client, store, checkpoint, acquire=bucket.acquire, page_size=args.page_size
                )
            except BackfillAbortedError as exc:
                print(json.dumps({"aborted": str(exc), **status_report(checkpoint)}))
                return EXIT_ABORTED
        print(json.dumps(status_report(checkpoint)))
        return EXIT_OK
    finally:
        await pool.close()


def main() -> None:
    configure_logging("feeds.ercot_backfill")
    raise SystemExit(asyncio.run(_async_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
