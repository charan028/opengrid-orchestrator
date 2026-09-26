"""Database I/O for `opengrid.invariants` (00-invariants.md K1/K2/K13, orphan bookkeeping). Isolated
from `invariants.checks`' pure logic per BUILD.md S5a ("pure logic separated from I/O"), mirroring
`opengrid.health.queries`'s split.

Every fetch here is bounded -- a `LIMIT`, an active-rows-only partial-index filter, or a rolling time
window -- never an unqualified scan of a whole table, so a run stays cheap as the fleet/obligation count
grows (see each function's docstring for its own bound). `opengrid.fleet.seed` at 2,000 hubs is the
reference scale these were sized against.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg_pool import AsyncConnectionPool

from opengrid.core.reasons import COMMIT_LOCK_OVERRIDE_REASONS, R_AS_RELEASE, R_SUBSTITUTION
from opengrid.invariants.models import CheckState, InvariantsSummary, Violation

#: 00-invariants.md K13's own exception list (never re-declared -- BUILD.md S1 "no duplicated
#: functions"): a grant dipping below its commitment's floor is not a lock violation if the trace
#: carries one of these reason codes for that obligation in that window.
ALLOWED_K13_TRACE_REASONS: frozenset[str] = COMMIT_LOCK_OVERRIDE_REASONS | {R_AS_RELEASE, R_SUBSTITUTION}

#: 02a S1.5 obligation states a live commitment-lock row must never survive on.
_ORPHAN_COMMITMENT_STATES = ("REJECTED", "EXPIRED")

_KW_TOLERANCE = 1e-6

# Per-run row caps: an invariants run is a background health-adjacent job, not a user-facing query --
# capping every fetch keeps one run's cost bounded and predictable regardless of backlog size. A
# backlog larger than the cap is simply picked up across more runs (the watermark carries the position
# forward), which is the whole point of incremental scanning.
_DEFAULT_BATCH_LIMIT = 5_000


async def fetch_reserve_breach_candidates(
    pool: AsyncConnectionPool, *, since: datetime, limit: int = _DEFAULT_BATCH_LIMIT
) -> tuple[list[tuple[str, datetime, float, float, float]], datetime | None]:
    """K1: telemetry rows newer than `since` where the hub was discharging (`p_kw < 0`), joined to its
    reserve floor. Bounded by the telemetry partition's own `ts` range plus `limit` -- never a scan of
    the whole (partitioned) telemetry table. Returns `(rows, new_watermark)`; `new_watermark` is the
    latest `ts` seen, or `None` if nothing new arrived since `since`."""
    sql = """
        SELECT t.hub_id, t.ts, t.soc_kwh, t.p_kw, h.r_kwh
        FROM og.telemetry t
        JOIN og.hub h ON h.hub_id = t.hub_id
        WHERE t.ts > %(since)s AND t.p_kw < 0
        ORDER BY t.ts
        LIMIT %(limit)s
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"since": since, "limit": limit})
        rows = await cur.fetchall()
    typed = [(r[0], r[1], float(r[2]), float(r[3]), float(r[4])) for r in rows]
    new_watermark = typed[-1][1] if typed else None
    return typed, new_watermark


async def fetch_reservation_aggregates(
    pool: AsyncConnectionPool, *, horizon_start: datetime
) -> list[tuple[str, datetime, datetime, float, float]]:
    """K2: active `POWER_KW` reservations summed per (bank, interval) from `horizon_start` onward (a
    rolling lookback/lookahead window, not the reservation table's full history -- past-and-settled
    intervals carry no live one-buyer risk). Bounded by the partial index on `released_at IS NULL`
    (`ix_reservation_bank_interval`) plus the `interval_start` filter. Returns EVERY grouping in the
    window, violating or not -- `invariants.checks.find_double_sold` decides which exceed capability."""
    sql = """
        SELECT r.bank_id, r.interval_start, r.interval_end, SUM(r.amount)::float8, b.kva_rating
        FROM og.reservation r
        JOIN og.bank b ON b.bank_id = r.bank_id
        WHERE r.released_at IS NULL AND r.kind = 'POWER_KW' AND r.interval_start >= %(horizon_start)s
        GROUP BY r.bank_id, r.interval_start, r.interval_end, b.kva_rating
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"horizon_start": horizon_start})
        rows = await cur.fetchall()
    return [(r[0], r[1], r[2], float(r[3]), float(r[4])) for r in rows]


async def fetch_lock_candidates(
    pool: AsyncConnectionPool, *, since: datetime, now: datetime, limit: int = _DEFAULT_BATCH_LIMIT
) -> tuple[list[tuple[str, datetime, datetime, float, float, bool]], datetime | None]:
    """K13: commitments whose interval has fully elapsed (`interval_end <= now`) since the last
    watermark, with the lowest grant seen for that obligation during the interval, and whether the
    trace carries an allowed override/substitution reason for that obligation in that window.

    Two bounded queries: the commitment/grant join is capped by `interval_end` range + `limit`; the
    trace lookup (`ALLOWED_K13_TRACE_REASONS`, via the GIN index on `reason_codes`) runs only for
    candidates that actually dipped below their committed floor -- typically a small subset, if any --
    never for every commitment in the batch. Non-dipped rows are returned with `has_allowed_reason=True`
    (harmless: `find_lock_violations` never flags a row that didn't dip, regardless of that flag).
    Returns `(rows, new_watermark)`; `new_watermark` is the latest `interval_end` seen.
    """
    sql = """
        SELECT c.obligation_id, c.interval_start, c.interval_end, c.committed_kw,
               COALESCE(MIN(g.granted_kw), 0)::float8 AS min_granted_kw
        FROM og.commitment c
        LEFT JOIN og.grant g
            ON g.obligation_id = c.obligation_id
           AND g.created_at >= c.interval_start AND g.created_at < c.interval_end
        WHERE c.supersedes IS NULL AND c.interval_end <= %(now)s AND c.interval_end > %(since)s
        GROUP BY c.obligation_id, c.interval_start, c.interval_end, c.committed_kw
        ORDER BY c.interval_end
        LIMIT %(limit)s
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"now": now, "since": since, "limit": limit})
        rows = await cur.fetchall()

    results: list[tuple[str, datetime, datetime, float, float, bool]] = []
    for obligation_id, interval_start, interval_end, committed_kw, min_granted_kw in rows:
        committed_kw = float(committed_kw)
        min_granted_kw = float(min_granted_kw)
        dipped = min_granted_kw < committed_kw - _KW_TOLERANCE
        has_allowed_reason = (
            True
            if not dipped
            else await _trace_has_allowed_reason(
                pool,
                obligation_id=str(obligation_id),
                window_start=interval_start,
                window_end=interval_end,
            )
        )
        results.append(
            (
                str(obligation_id),
                interval_start,
                interval_end,
                committed_kw,
                min_granted_kw,
                has_allowed_reason,
            )
        )
    new_watermark = results[-1][2] if results else None
    return results, new_watermark


async def _trace_has_allowed_reason(
    pool: AsyncConnectionPool, *, obligation_id: str, window_start: datetime, window_end: datetime
) -> bool:
    sql = """
        SELECT EXISTS (
            SELECT 1 FROM og.trace
            WHERE reason_codes && %(allowed)s
              AND scope ->> 'obligation_id' = %(obligation_id)s
              AND created_at >= %(window_start)s AND created_at < %(window_end)s
        )
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql,
            {
                "allowed": list(ALLOWED_K13_TRACE_REASONS),
                "obligation_id": obligation_id,
                "window_start": window_start,
                "window_end": window_end,
            },
        )
        row = await cur.fetchone()
    return bool(row[0]) if row is not None else False


async def fetch_orphan_reservations(
    pool: AsyncConnectionPool, *, limit: int = _DEFAULT_BATCH_LIMIT
) -> list[tuple[str, str, str, datetime]]:
    """An active reservation with no matching active commitment for its obligation/interval (see
    `invariants.checks.find_orphan_reservations`). Bounded by the `released_at IS NULL` partial index
    (the same set `opengrid.ledger.release_uncommitted` scans) plus `limit`."""
    sql = """
        SELECT r.reservation_id, r.obligation_id, r.bank_id, r.interval_start
        FROM og.reservation r
        WHERE r.released_at IS NULL
          AND NOT EXISTS (
              SELECT 1 FROM og.commitment c
              WHERE c.obligation_id = r.obligation_id
                AND c.interval_start = r.interval_start
                AND c.supersedes IS NULL
          )
        LIMIT %(limit)s
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"limit": limit})
        rows = await cur.fetchall()
    return [(str(r[0]), str(r[1]), r[2], r[3]) for r in rows]


async def fetch_orphan_commitments(
    pool: AsyncConnectionPool, *, limit: int = _DEFAULT_BATCH_LIMIT
) -> list[tuple[str, str, datetime, str]]:
    """An active commitment (`supersedes IS NULL`) whose obligation has reached a terminal,
    non-fulfilling state (see `invariants.checks.find_orphan_commitments`). Bounded by `limit`; the
    join is on primary keys."""
    sql = """
        SELECT c.commitment_id, c.obligation_id, c.interval_start, o.state
        FROM og.commitment c
        JOIN og.obligation o ON o.obligation_id = c.obligation_id
        WHERE c.supersedes IS NULL AND o.state = ANY(%(states)s)
        LIMIT %(limit)s
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"states": list(_ORPHAN_COMMITMENT_STATES), "limit": limit})
        rows = await cur.fetchall()
    return [(str(r[0]), str(r[1]), r[2], r[3]) for r in rows]


async def fetch_trace_stream_ids(pool: AsyncConnectionPool) -> list[str]:
    """Every distinct `stream_id` ever written to `og.trace` -- read-only, used only to know which
    streams `opengrid.trace.store.TraceStore.verify` should be run against (that store owns all hashing/
    chain-walking; this is not a substitute for its own `stream_ids()`, just this package's own way to
    discover the set without reaching into `TraceStore`'s private backend, which is out of scope for
    this package to touch -- BUILD.md S4 directory ownership)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT DISTINCT stream_id FROM og.trace")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def fetch_trace_max_seq(pool: AsyncConnectionPool, stream_id: str) -> int | None:
    """The highest `seq` currently stored for `stream_id`, or `None` if it has none -- used to advance
    `TRACE_VERIFY`'s per-stream watermark past whatever a clean `verify()` run just covered."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT MAX(seq) FROM og.trace WHERE stream_id = %(stream_id)s", {"stream_id": stream_id}
        )
        row = await cur.fetchone()
    return int(row[0]) if row is not None and row[0] is not None else None


# --- og.invariant_check / og.invariant_violation persistence ---------------------------------------

_SELECT_CHECK_STATE_SQL = """
    SELECT check_name, last_run_at, last_run_ms, last_violations, total_violations, watermark
    FROM og.invariant_check WHERE check_name = %(check_name)s
"""

_UPSERT_CHECK_STATE_SQL = """
    INSERT INTO og.invariant_check
        (check_name, last_run_at, last_run_ms, last_violations, total_violations, watermark, updated_at)
    VALUES (%(check_name)s, %(last_run_at)s, %(last_run_ms)s, %(last_violations)s, %(total_violations)s,
            %(watermark)s, %(updated_at)s)
    ON CONFLICT (check_name) DO UPDATE SET
        last_run_at = EXCLUDED.last_run_at, last_run_ms = EXCLUDED.last_run_ms,
        last_violations = EXCLUDED.last_violations, total_violations = EXCLUDED.total_violations,
        watermark = EXCLUDED.watermark, updated_at = EXCLUDED.updated_at
"""


async def get_check_state(pool: AsyncConnectionPool, check_name: str) -> CheckState:
    """The durable watermark/totals for `check_name`, or `CheckState.empty` if it has never run
    (first run after this migration, or a fresh database)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_SELECT_CHECK_STATE_SQL, {"check_name": check_name})
        row = await cur.fetchone()
    if row is None:
        return CheckState.empty(check_name)
    return CheckState(
        check_name=row[0],
        last_run_at=row[1],
        last_run_ms=row[2],
        last_violations=row[3],
        total_violations=float(row[4]),
        watermark=dict(row[5]) if row[5] else {},
    )


async def upsert_check_state(
    pool: AsyncConnectionPool,
    check_name: str,
    *,
    ran_at: datetime,
    run_ms: int,
    violation_count: int,
    total_violations: float,
    watermark: dict[str, Any],
) -> None:
    from psycopg.types.json import Jsonb  # local import: only this write path needs the jsonb adapter

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _UPSERT_CHECK_STATE_SQL,
            {
                "check_name": check_name,
                "last_run_at": ran_at,
                "last_run_ms": run_ms,
                "last_violations": violation_count,
                "total_violations": total_violations,
                "watermark": Jsonb(watermark),
                "updated_at": ran_at,
            },
        )
        await conn.commit()


async def insert_violations(pool: AsyncConnectionPool, check_name: str, violations: list[Violation]) -> None:
    """Append each violation to `og.invariant_violation` (the audit trail) in one batched statement.
    A no-op for an empty list (the common case: a healthy run finds nothing)."""
    if not violations:
        return
    from psycopg.types.json import Jsonb  # local import: only this write path needs the jsonb adapter

    now = datetime.now(UTC)
    sql = """
        INSERT INTO og.invariant_violation (check_name, detected_at, scope, detail)
        VALUES (%(check_name)s, %(detected_at)s, %(scope)s, %(detail)s)
    """
    params = [
        {
            "check_name": check_name,
            "detected_at": now,
            "scope": Jsonb(v.scope),
            "detail": Jsonb(v.detail),
        }
        for v in violations
    ]
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.executemany(sql, params)
        await conn.commit()


_SELECT_SUMMARY_SQL = (
    "SELECT check_name, last_run_at, total_violations, last_violations FROM og.invariant_check"
)


async def read_summary(pool: AsyncConnectionPool) -> InvariantsSummary:
    """The measured read model `api/routers/health.py`/`dispatch.py` expose: a plain read of
    `og.invariant_check`, never a constant. A check that has never run contributes 0 and does not affect
    `as_of` (there is nothing yet to be "as of").

    K1/K2/K13 are reported as their running TOTAL (`total_violations`) -- Prometheus-counter-shaped,
    matching `og_reserve_breaches_total`/`og_double_sold_kwh_total`/`og_lock_violations_total`, which
    "must stay 0" for the life of the deployment, not just the latest run. The two orphan checks are
    reported as the count found in the MOST RECENT run (`last_violations`) -- they describe current
    outstanding bookkeeping debt (a gauge), which can legitimately go back down once cleaned up, unlike
    a breach count.
    """
    from opengrid.invariants.models import (
        CHECK_K1_RESERVE_BREACH,
        CHECK_K2_DOUBLE_SOLD,
        CHECK_K13_LOCK_VIOLATION,
        CHECK_ORPHAN_COMMITMENT,
        CHECK_ORPHAN_RESERVATION,
    )

    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_SELECT_SUMMARY_SQL)
        rows = await cur.fetchall()
    totals = {r[0]: float(r[2]) for r in rows}
    last_counts = {r[0]: int(r[3]) for r in rows}
    run_ats = [r[1] for r in rows if r[1] is not None]
    return InvariantsSummary(
        reserve_breaches=int(totals.get(CHECK_K1_RESERVE_BREACH, 0)),
        double_sold_kwh=totals.get(CHECK_K2_DOUBLE_SOLD, 0.0),
        lock_violations=int(totals.get(CHECK_K13_LOCK_VIOLATION, 0)),
        orphan_reservations=last_counts.get(CHECK_ORPHAN_RESERVATION, 0),
        orphan_commitments=last_counts.get(CHECK_ORPHAN_COMMITMENT, 0),
        as_of=min(run_ats) if run_ats else None,
    )
