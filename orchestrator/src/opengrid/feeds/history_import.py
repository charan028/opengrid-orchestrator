"""CLI: import `/var/lib/opengrid/import/mariadb_history_signals.tsv` into `og.feed_obs` as
`source='HIST'`, so `opengrid.forecast` has 2.5 days of history to seed quantiles from on a cold start
(BUILD.md S4 "feeds agent" scope item 2).

Idempotent: `og.feed_obs`'s natural key `(source, product, series, ts)` means re-running the import
overwrites rows in place rather than duplicating them (same upsert `FeedStore.upsert_obs` uses for live
polling, BUILD.md S1 "no duplicated functions").

**Mapping documented here** (the two `signal_type` values this importer consumes; every other row in
the TSV -- `grid_stress_price_mwh`, line flows, etc. -- is out of MVP-S forecast scope and skipped):

| TSV `signal_type`     | `feed_obs.product` | `feed_obs.series` | `feed_obs.unit` (from TSV `unit`) |
|------------------------|---------------------|--------------------|-------------------------------------|
| `wholesale_price_mwh`  | `hist-price`        | TSV `asset`        | as given (expected `usd_per_mwh`)   |
| `substation_load_kw`   | `hist-load`         | TSV `asset`        | as given (expected `kw`)            |

`feed_obs.quality` is taken from the TSV `quality` column, normalized to `GOOD`/`ESTIMATED`/`STALE`
(anything else maps to `ESTIMATED`, since a historical import is never as trustworthy as a live GOOD
reading and the schema has no `QUARANTINED` value to fall back to).
"""

from __future__ import annotations

import argparse
import asyncio
import csv
import logging
import sys
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

from opengrid.core.models.platform import FeedObs
from opengrid.feeds.store import FeedStore
from opengrid.platform.config import load_config
from opengrid.platform.db import make_pool
from opengrid.platform.log import configure_logging

logger = logging.getLogger(__name__)

DEFAULT_TSV_PATH = Path("/var/lib/opengrid/import/mariadb_history_signals.tsv")
SOURCE_HIST = "HIST"
BATCH_SIZE = 2000

_SIGNAL_TYPE_TO_PRODUCT = {
    "wholesale_price_mwh": "hist-price",
    "substation_load_kw": "hist-load",
}

_QUALITY_MAP: dict[str, Literal["GOOD", "ESTIMATED", "STALE"]] = {
    "GOOD": "GOOD",
    "ESTIMATED": "ESTIMATED",
    "STALE": "STALE",
}


def _normalize_quality(raw: str) -> Literal["GOOD", "ESTIMATED", "STALE"]:
    return _QUALITY_MAP.get(raw.strip().upper(), "ESTIMATED")


def _parse_reading_time(raw: str) -> datetime:
    dt = datetime.fromisoformat(raw.strip())
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def iter_feed_obs(tsv_path: Path, *, recorded_at: datetime) -> Iterator[FeedObs]:
    """Stream `FeedObs` rows for the signal types this importer maps (see module docstring)."""
    with tsv_path.open(newline="", encoding="utf-8") as fh:
        reader = csv.DictReader(fh, delimiter="\t")
        for row_num, row in enumerate(reader, start=2):  # header is line 1
            signal_type = row.get("signal_type", "").strip()
            product = _SIGNAL_TYPE_TO_PRODUCT.get(signal_type)
            if product is None:
                continue
            try:
                yield FeedObs(
                    source=SOURCE_HIST,
                    product=product,
                    series=row["asset"].strip(),
                    ts=_parse_reading_time(row["reading_time_utc"]),
                    value=float(row["value"]),
                    unit=row["unit"].strip(),
                    quality=_normalize_quality(row.get("quality", "ESTIMATED")),
                    recorded_at=recorded_at,
                )
            except (KeyError, ValueError) as exc:
                logger.warning("skipping malformed history row", extra={"line": row_num, "error": str(exc)})


def _chunk(rows: Iterator[FeedObs], size: int) -> Iterator[list[FeedObs]]:
    batch: list[FeedObs] = []
    for row in rows:
        batch.append(row)
        if len(batch) >= size:
            yield batch
            batch = []
    if batch:
        yield batch


async def run_import(tsv_path: Path, store: FeedStore, *, recorded_at: datetime | None = None) -> int:
    """Import `tsv_path` into `feed_obs` via `store`. Returns the number of rows written."""
    recorded_at = recorded_at or datetime.now(UTC)
    total = 0
    for batch in _chunk(iter_feed_obs(tsv_path, recorded_at=recorded_at), BATCH_SIZE):
        await store.upsert_obs(batch)
        total += len(batch)
        logger.info("imported history batch", extra={"batch_rows": len(batch), "total_rows": total})
    return total


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(prog="python -m opengrid.feeds.history_import")
    parser.add_argument("--tsv-path", type=Path, default=DEFAULT_TSV_PATH)
    parser.add_argument("--config", default=None, help="Path to orchestrator.toml (defaults to OG_CONFIG)")
    return parser.parse_args(argv)


async def _async_main(argv: list[str]) -> int:
    args = _parse_args(argv)
    if not args.tsv_path.exists():
        logger.error("history TSV not found", extra={"path": str(args.tsv_path)})
        return 1

    cfg = load_config(args.config)
    pool = await make_pool(cfg)
    try:
        store = FeedStore(pool, staleness_cfg=cfg.get("feeds.staleness", {}))
        total = await run_import(args.tsv_path, store)
    finally:
        await pool.close()

    logger.info("history import complete", extra={"rows": total})
    return 0


def main() -> None:
    configure_logging("feeds.history_import")
    raise SystemExit(asyncio.run(_async_main(sys.argv[1:])))


if __name__ == "__main__":
    main()
