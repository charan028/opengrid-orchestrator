"""Scheduled trace-chain verification (00-invariants.md K11: "Property test: verify after random
prune" is the offline half of this invariant's proof; this is the live half -- a periodic re-derivation
of every stream's hash chain, not just the on-demand `POST /og/api/trace/verify` a human triggers from
the billing/audit screen, `api/routers/billing.py`).

Reuses `opengrid.trace.store.TraceStore.verify` (built on `opengrid.core.tracehash`) exactly as the
on-demand endpoint does -- this module never re-implements hashing or chain-walking, only decides WHEN
to check, WHICH streams exist (`invariants.queries.fetch_trace_stream_ids`, a plain read of `og.trace`
rather than reaching into `TraceStore`'s own private backend, which belongs to a different owner -- see
`fetch_trace_stream_ids`'s docstring), and WHAT to do with a failure.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from opengrid.health.model import AlertFinding
from opengrid.health.queries import clear_alert, condition_key_for, fetch_open_alerts, raise_alert
from opengrid.invariants import queries as invariants_queries
from opengrid.invariants.models import CHECK_TRACE_VERIFY
from opengrid.trace.store import TraceStore

logger = logging.getLogger(__name__)

ALR_TRACE_VERIFY_FAILED = "ALR-TRACE-VERIFY-FAILED"


@dataclass(frozen=True, slots=True)
class TraceVerifyOutcome:
    checked_streams: int
    failed_streams: tuple[str, ...]
    new_watermark: dict[str, int]  # stream_id -> next from_seq to resume from (last verified seq + 1)


async def verify_new_segments(
    pool: AsyncConnectionPool, trace_store: TraceStore, *, watermark: dict[str, Any]
) -> TraceVerifyOutcome:
    """Verify, for every known stream, the segment from this check's last-verified `seq` onward
    (`watermark[stream_id]`, or from the start for a stream never checked before). Bounded per run: only
    the NEW rows since the last check are re-hashed, not the whole chain every time -- the same
    checkpoint-relative resume `TraceStore.verify`'s `from_seq` already supports for pruned chains (02a
    S8.3) doubles as this check's own incremental scan.

    A stream that fails verification keeps its watermark at the last point it verified as broken (not
    advanced past the break), so the next run finds the same break again until it is fixed -- a
    verification failure must never be silently skipped past.
    """
    stream_ids = await invariants_queries.fetch_trace_stream_ids(pool)
    failed: list[str] = []
    new_watermark: dict[str, int] = dict(watermark)
    for stream_id in stream_ids:
        from_seq = int(watermark.get(stream_id, 0))
        result = await trace_store.verify(stream_id, from_seq=from_seq)
        if result.ok:
            max_seq = await invariants_queries.fetch_trace_max_seq(pool, stream_id)
            if max_seq is not None:
                new_watermark[stream_id] = max_seq + 1
        else:
            failed.append(stream_id)
            logger.error(
                "trace chain verification failed",
                extra={"stream_id": stream_id, "seq": result.broken_at_seq, "reason": result.reason},
            )
    return TraceVerifyOutcome(
        checked_streams=len(stream_ids), failed_streams=tuple(failed), new_watermark=new_watermark
    )


async def raise_or_clear_alert(
    pool: AsyncConnectionPool, outcome: TraceVerifyOutcome, *, now: datetime | None = None
) -> None:
    """Critical `ALR-TRACE-VERIFY-FAILED` through the same `og.alert` mechanism `opengrid.health` uses
    (BUILD.md task brief): raised once per condition (no duplicate storm, matching health's own
    raise-once/clear-on-resolve rule, `health.evaluate_alerts`), cleared once every stream verifies
    clean again."""
    now = now or datetime.now(UTC)
    open_alerts = await fetch_open_alerts(pool)
    open_by_key = {condition_key_for(a): a for a in open_alerts if a.rule == ALR_TRACE_VERIFY_FAILED}

    if outcome.failed_streams:
        condition_key = f"{ALR_TRACE_VERIFY_FAILED}:{','.join(sorted(outcome.failed_streams))}"
        if condition_key not in open_by_key:
            await raise_alert(
                pool,
                AlertFinding(
                    rule=ALR_TRACE_VERIFY_FAILED,
                    severity="critical",
                    summary=(
                        f"Trace chain verification failed for stream(s): {', '.join(outcome.failed_streams)}"
                    ),
                    condition_key=condition_key,
                    detail={
                        "failed_streams": list(outcome.failed_streams),
                        "checked": outcome.checked_streams,
                    },
                ),
                opened_at=now,
            )
    else:
        for alert in open_by_key.values():
            if alert.id is not None:
                await clear_alert(pool, alert.id, cleared_at=now)


__all__ = [
    "ALR_TRACE_VERIFY_FAILED",
    "CHECK_TRACE_VERIFY",
    "TraceVerifyOutcome",
    "raise_or_clear_alert",
    "verify_new_segments",
]
