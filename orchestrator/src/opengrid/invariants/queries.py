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
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.core.reasons import COMMIT_LOCK_OVERRIDE_REASONS, R_AS_RELEASE, R_SUBSTITUTION
from opengrid.invariants import checks
from opengrid.invariants.models import CheckState, InvariantsSummary, Violation

#: 00-invariants.md K13's own exception list (never re-declared -- BUILD.md S1 "no duplicated
#: functions"): a grant dipping below its commitment's floor is not a lock violation if the trace
#: carries one of these reason codes for that obligation in that window.
ALLOWED_K13_TRACE_REASONS: frozenset[str] = COMMIT_LOCK_OVERRIDE_REASONS | {R_AS_RELEASE, R_SUBSTITUTION}

#: 02a S1.5 obligation states a live commitment-lock row must never survive on.
_ORPHAN_COMMITMENT_STATES = ("REJECTED", "EXPIRED")

# Per-run row caps: an invariants run is a background health-adjacent job, not a user-facing query --
# capping every fetch keeps one run's cost bounded and predictable regardless of backlog size. A
# backlog larger than the cap is simply picked up across more runs (the watermark carries the position
# forward), which is the whole point of incremental scanning.
_DEFAULT_BATCH_LIMIT = 5_000


# --- K1: reserve breach -----------------------------------------------------------------------------


async def fetch_reserve_breach_candidates(
    pool: AsyncConnectionPool,
    *,
    since_ts: datetime,
    since_hub_id: str,
    upper_bound: datetime,
    limit: int = _DEFAULT_BATCH_LIMIT,
) -> tuple[list[tuple[str, datetime, float, float, float]], tuple[datetime, str] | None]:
    """K1: telemetry rows strictly after the compound cursor `(since_ts, since_hub_id)` and at or before
    `upper_bound`, where the hub was discharging (`p_kw < 0`), joined to its reserve floor.

    Compound cursor, not a bare `ts > since` (adversarial-review fix): telemetry is written in batches
    where many hubs share the exact same `ts` (`opengrid.fleet`'s flush cadence). A strict `ts > since`
    watermark set to the last-seen `ts` silently skips every OTHER row still sitting at that same `ts`
    once a batch spans more than one `LIMIT`-sized fetch. Ordering and comparing on the row constructor
    `(ts, hub_id)` (both indexed via the partition's own ordering plus `hub_id`'s natural sort) makes the
    cursor gapless regardless of how many hubs share a timestamp.

    `upper_bound` (caller passes `now - k1_safety_margin_s`) deliberately holds back the most recent
    slice of time so a row that arrives late (buffered writes, clock skew) still lands before the cursor
    ever passes it -- advancing a watermark all the way to `now()` risks permanently skipping a write
    that hadn't committed yet at read time.

    Bounded by the telemetry partition's own `ts` range plus `limit` -- never a scan of the whole
    (partitioned) telemetry table. Returns `(rows, new_cursor)`; `new_cursor` is `(ts, hub_id)` of the
    last row returned, or `None` if nothing new arrived in range. Callers loop this (see
    `opengrid.invariants._run_reserve_breach_check`) until either a fetch returns fewer than `limit` rows
    (caught up to `upper_bound`) or a time budget elapses -- a single un-looped batch here falls
    behind indefinitely once discharge events arrive faster than `limit` per run interval.
    """
    sql = """
        SELECT t.hub_id, t.ts, t.soc_kwh, t.p_kw, h.r_kwh
        FROM og.telemetry t
        JOIN og.hub h ON h.hub_id = t.hub_id
        WHERE (t.ts, t.hub_id) > (%(since_ts)s, %(since_hub_id)s)
          AND t.ts <= %(upper_bound)s
          AND t.p_kw < 0
        ORDER BY t.ts, t.hub_id
        LIMIT %(limit)s
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql,
            {"since_ts": since_ts, "since_hub_id": since_hub_id, "upper_bound": upper_bound, "limit": limit},
        )
        rows = await cur.fetchall()
    typed = [(r[0], r[1], float(r[2]), float(r[3]), float(r[4])) for r in rows]
    new_cursor = (typed[-1][1], typed[-1][0]) if typed else None
    return typed, new_cursor


# --- K2: double-sold kWh -----------------------------------------------------------------------------


async def fetch_reservation_aggregates(
    pool: AsyncConnectionPool, *, horizon_start: datetime
) -> list[tuple[str, datetime, datetime, float]]:
    """K2: active `POWER_KW` reservations summed per (bank, interval) from `horizon_start` onward (a
    rolling lookback/lookahead window, not the reservation table's full history -- past-and-settled
    intervals carry no live one-buyer risk). Bounded by the partial index on `released_at IS NULL`
    (`ix_reservation_bank_interval`) plus the `interval_start` filter. Returns EVERY grouping in the
    window, violating or not -- `invariants.checks.find_double_sold` (given `compute_bank_capabilities_kw`'s
    result) decides which exceed the bank's TRUE capability."""
    sql = """
        SELECT r.bank_id, r.interval_start, r.interval_end, SUM(r.amount)::float8
        FROM og.reservation r
        WHERE r.released_at IS NULL AND r.kind = 'POWER_KW' AND r.interval_start >= %(horizon_start)s
        GROUP BY r.bank_id, r.interval_start, r.interval_end
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"horizon_start": horizon_start})
        rows = await cur.fetchall()
    return [(r[0], r[1], r[2], float(r[3])) for r in rows]


async def fetch_bank_capability_inputs(
    pool: AsyncConnectionPool,
) -> list[tuple[str, float, float, float, float, float, float, float, float, str]]:
    """K2's capability inputs (adversarial-review fix): one row per hub, everything
    `checks.compute_bank_capabilities_kw` needs to reproduce the ledger's own admission-time capability
    formula (`opengrid.core.physics.hub_capability`/`bank_capability`) from persisted state, instead of
    the bank's static `kva_rating` nameplate the old check compared against (nameplate over-states real
    capability whenever any hub is offline or low on SoC, so a double-sale that stayed under nameplate
    but over the real ceiling went undetected).

    Bounded by hub count, which is fixed by the fleet's own size (`opengrid.fleet.seed`) -- this does NOT
    grow with reservation/obligation volume the way the rest of this package's fetches are bounded by a
    rolling window; a fleet's hub count is its own separate, comparatively static scale.

    `(bank_id, kva_rating, reserve_kva, e_kwh, r_kwh, p_kw, eta_c, eta_d, soc_kwh, health)` per hub.
    """
    sql = """
        SELECT h.bank_id, b.kva_rating, b.reserve_kva, h.e_kwh, h.r_kwh, h.p_kw, h.eta_c, h.eta_d,
               hs.soc_kwh, hs.health
        FROM og.hub h
        JOIN og.bank b ON b.bank_id = h.bank_id
        JOIN og.hub_state hs ON hs.hub_id = h.hub_id
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql)
        rows = await cur.fetchall()
    return [
        (
            r[0],
            float(r[1]),
            float(r[2]),
            float(r[3]),
            float(r[4]),
            float(r[5]),
            float(r[6]),
            float(r[7]),
            float(r[8]),
            r[9],
        )
        for r in rows
    ]


# --- K13: commitment-lock violation / outage gap ------------------------------------------------------


async def fetch_lock_commitment_candidates(
    pool: AsyncConnectionPool, *, since: datetime, now: datetime, limit: int = _DEFAULT_BATCH_LIMIT
) -> tuple[list[tuple[UUID, datetime, datetime, float]], datetime | None]:
    """K13: active commitments (`supersedes IS NULL`) whose interval has fully elapsed
    (`interval_end <= now`) since the last watermark -- one row per (obligation, interval), bounded by
    the `interval_end` range plus `limit`. Callers combine each candidate with its own
    `fetch_grant_cycle_series`/`fetch_covering_trace_info` (`invariants.checks.find_dip` decides whether
    it actually dipped) -- this function only enumerates WHICH commitments need checking. Returns
    `(rows, new_watermark)`; `new_watermark` is the latest `interval_end` seen.
    """
    sql = """
        SELECT c.obligation_id, c.interval_start, c.interval_end, c.committed_kw
        FROM og.commitment c
        WHERE c.supersedes IS NULL AND c.interval_end <= %(now)s AND c.interval_end > %(since)s
        ORDER BY c.interval_end
        LIMIT %(limit)s
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"now": now, "since": since, "limit": limit})
        rows = await cur.fetchall()
    results = [(r[0], r[1], r[2], float(r[3])) for r in rows]
    new_watermark = results[-1][2] if results else None
    return results, new_watermark


async def fetch_need_basis_obligation_ids(
    pool: AsyncConnectionPool, obligation_ids: list[UUID]
) -> frozenset[str]:
    """K13 need-basis exemption (00-invariants.md, owner decision 2026-09-26): which of `obligation_ids`
    belong to a contract whose CURRENT (highest-version) service profile has `setpoint_source =
    'MEASURED_FEEDBACK'` (DATA_CENTER/PIPELINE_AC's closed-loop profiles) -- for these, the committed kW
    is a reserved maximum, not a fixed schedule (`checks.classify_dip`'s `is_need_basis`). Bounded by
    `obligation_ids` (this run's own K13 candidate batch), never a scan of `og.service_profile` on its
    own. An obligation with no service profile row at all is not need-basis (fixed-schedule is the
    default)."""
    if not obligation_ids:
        return frozenset()
    sql = """
        SELECT DISTINCT o.obligation_id
        FROM og.obligation o
        JOIN og.contract c ON c.contract_id = o.contract_id
        JOIN og.service_profile sp ON sp.contract_id = c.contract_id
        WHERE o.obligation_id = ANY(%(obligation_ids)s)
          AND sp.setpoint_source = 'MEASURED_FEEDBACK'
          AND sp.version = (
              SELECT MAX(sp2.version) FROM og.service_profile sp2 WHERE sp2.contract_id = c.contract_id
          )
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"obligation_ids": obligation_ids})
        rows = await cur.fetchall()
    return frozenset(str(r[0]) for r in rows)


async def fetch_grant_cycle_series(
    pool: AsyncConnectionPool, *, obligation_id: UUID, window_start: datetime, window_end: datetime
) -> list[tuple[datetime, float, str]]:
    """K13: one `(timestamp, total_kw, cycle_id)` sample per allocator cycle (`og.grant.cycle_id`) for
    `obligation_id` inside `[window_start, window_end)`, `total_kw` already SUMMED ACROSS EVERY BANK
    that cycle granted to -- K13 is defined on the OBLIGATION's total delivery against its committed
    floor (00-invariants.md), not on any one bank's share of it (verified live 2026-09-26: comparing a
    single bank's grant row to the whole obligation's `committed_kw` falsely flagged an ERCOT_AS
    obligation whose banks summed exactly to its commitment). `is_headroom` grants are excluded -- they
    are uncommitted spot capacity, not delivery against this obligation. Sorted by timestamp ascending
    for `checks.find_dip`'s gap-scan; `cycle_id` lets `checks._dip_is_covered` match a dip to a covering
    trace by the SAME allocator cycle (see that function's docstring for why timestamp alone is not
    sufficient for the cycle that starts a shortfall)."""
    sql = """
        SELECT MIN(g.created_at) AS ts, SUM(g.granted_kw)::float8 AS total_kw, g.cycle_id
        FROM og.grant g
        WHERE g.obligation_id = %(obligation_id)s AND g.is_headroom = false
          AND g.created_at >= %(window_start)s AND g.created_at < %(window_end)s
        GROUP BY g.cycle_id
        ORDER BY ts
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql, {"obligation_id": obligation_id, "window_start": window_start, "window_end": window_end}
        )
        rows = await cur.fetchall()
    return [(r[0], float(r[1]), r[2]) for r in rows]


async def fetch_covering_trace_info(
    pool: AsyncConnectionPool, *, obligation_id: UUID, window_start: datetime, window_end: datetime
) -> tuple[datetime | None, frozenset[str]]:
    """K13: the earliest time in `[window_start, window_end)` at which the trace already carried an
    allowed override/substitution/shortfall reason (`ALLOWED_K13_TRACE_REASONS`) for `obligation_id`,
    AND the set of allocator `cycle_id`s those trace rows carry in their own payload (for
    `checks._dip_is_covered`'s same-cycle epsilon -- `opengrid.allocator.__init__.run_cycle` persists a
    cycle's grants BEFORE it traces that cycle's shortfall, so the trace's `created_at` is a few
    milliseconds AFTER the grant's for the very cycle that started the shortfall; matching by `cycle_id`
    sidesteps that write-order race for the common same-cycle case). Returns `(None, frozenset())` if no
    covering trace exists in the window.

    The engine/allocator (`opengrid.engine.gateways.EngineLedgerGateway.record_shortfalls`/
    `record_substitution_events`) trace these with the obligation id AND (for `record_shortfalls`/
    `record_substitution_events`, not the manual `record_substitution`) the cycle id in the trace row's
    `payload` -- `og.trace.scope` is never populated by any writer (verified live 2026-09-26:
    `TraceStore.append` takes no `scope` argument at all), so a lookup against `scope` can never match
    anything a real K13 exception ever writes. `reason_codes && ALLOWED_K13_TRACE_REASONS` uses the GIN
    index on `reason_codes` (`ix_trace_reason`) to keep this cheap; it also runs only for candidates
    `checks.find_dip` already found dipping, never for every commitment.
    """
    sql = """
        SELECT MIN(created_at), array_remove(array_agg(DISTINCT payload ->> 'cycle_id'), NULL)
        FROM og.trace
        WHERE reason_codes && %(allowed)s
          AND payload ->> 'obligation_id' = %(obligation_id)s
          AND created_at >= %(window_start)s AND created_at < %(window_end)s
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql,
            {
                "allowed": list(ALLOWED_K13_TRACE_REASONS),
                "obligation_id": str(obligation_id),
                "window_start": window_start,
                "window_end": window_end,
            },
        )
        row = await cur.fetchone()
    if row is None or row[0] is None:
        return None, frozenset()
    return row[0], frozenset(row[1] or [])


async def fetch_shortfall_events(
    pool: AsyncConnectionPool, *, obligation_id: UUID, window_start: datetime, window_end: datetime
) -> list[checks.ShortfallEvent]:
    """K13 best-effort shortfall / K13_RESTORE_LAG (owner decision 2026-09-26): every traced SHORTFALL
    transition for `obligation_id` in `[window_start, window_end)`, with its `shortfall_kw` (the trace
    payload `opengrid.engine.gateways.EngineLedgerGateway.record_shortfalls` writes) -- a
    `shortfall_kw <= 0` event marks the constraint CLEARING. Sorted by time. Bounded the same way
    `fetch_covering_trace_info` is: the GIN index on `reason_codes`, scoped to one obligation/window."""
    sql = """
        SELECT created_at, (payload ->> 'shortfall_kw')::float8, payload ->> 'cycle_id'
        FROM og.trace
        WHERE decision_type = 'SHORTFALL'
          AND payload ->> 'obligation_id' = %(obligation_id)s
          AND created_at >= %(window_start)s AND created_at < %(window_end)s
        ORDER BY created_at
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            sql, {"obligation_id": str(obligation_id), "window_start": window_start, "window_end": window_end}
        )
        rows = await cur.fetchall()
    return [checks.ShortfallEvent(at=r[0], shortfall_kw=float(r[1] or 0.0), cycle_id=r[2]) for r in rows]


async def fetch_measured_need_sample(
    pool: AsyncConnectionPool, *, obligation_id: UUID, at: datetime
) -> checks.MeasuredNeedSample | None:
    """K13 need-basis violation (b) (owner decision 2026-09-26): the customer's own measured reading
    behind the obligation's service profile `feedback_signal_ref`
    (`opengrid.site_ingest.latest.parse_feedback_ref`'s two forms, `site_meter:<site_id>:<field>` /
    `corridor:<corridor_id>:<field>`), at or before `at` (the dip's own timestamp -- the reading in
    effect when the dip happened, not "whatever is latest now"). `None` if the obligation has no service
    profile, an unparseable/unsupported ref, or no reading has ever arrived for that source.

    Bounded to one obligation's contract/profile lookup plus a single most-recent-row-at-or-before-`at`
    read per table (`ix_site_meter_reading_ts`/`ix_corridor_current_reading_ts`), never a scan proportional
    to reading volume.
    """
    sql_profile = """
        SELECT c.customer_id::text, sp.feedback_signal_ref
        FROM og.obligation o
        JOIN og.contract c ON c.contract_id = o.contract_id
        JOIN og.service_profile sp ON sp.contract_id = c.contract_id
        WHERE o.obligation_id = %(obligation_id)s
          AND sp.version = (
              SELECT MAX(sp2.version) FROM og.service_profile sp2 WHERE sp2.contract_id = c.contract_id
          )
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql_profile, {"obligation_id": obligation_id})
        row = await cur.fetchone()
    if row is None or row[1] is None:
        return None
    customer_id, feedback_signal_ref = row[0], row[1]

    try:  # opengrid.site_ingest ships with the customer-services package; absent, there is no need basis
        from opengrid.site_ingest.latest import (  # type: ignore[import-untyped,import-not-found,unused-ignore]
            FeedbackRefError,
            parse_feedback_ref,
        )
    except ImportError:
        return None

    try:
        ref = parse_feedback_ref(feedback_signal_ref)
    except FeedbackRefError:
        return None

    if ref.kind == "site_meter":
        sql = """
            SELECT p_kw, quality FROM og.customer_site_meter_reading
            WHERE customer_id = %(customer_id)s AND site_id = %(source_id)s AND ts <= %(at)s
            ORDER BY ts DESC LIMIT 1
        """
    else:
        sql = """
            SELECT i_ac_a, limit_a, quality FROM og.corridor_current_reading
            WHERE customer_id = %(customer_id)s AND corridor_id = %(source_id)s AND ts <= %(at)s
            ORDER BY ts DESC LIMIT 1
        """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"customer_id": customer_id, "source_id": ref.source_id, "at": at})
        reading_row = await cur.fetchone()
    if reading_row is None:
        return None
    if ref.kind == "site_meter":
        return checks.MeasuredNeedSample(
            kind="site_meter",
            field=ref.field,
            value=float(reading_row[0]),
            limit=None,
            quality=reading_row[1],
        )
    return checks.MeasuredNeedSample(
        kind="corridor",
        field=ref.field,
        value=float(reading_row[0]),
        limit=float(reading_row[1]),
        quality=reading_row[2],
    )


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


# --- K11: scheduled trace verification -- per-stream watermark rows ---------------------------------


async def fetch_known_trace_stream_ids(pool: AsyncConnectionPool) -> list[str]:
    """Every stream `opengrid.invariants` has already discovered (`og.invariant_trace_watermark`),
    bounded by the number of DISTINCT streams -- not `og.trace`'s row count, unlike a raw
    `SELECT DISTINCT stream_id FROM og.trace` (adversarial-review fix: that scan's cost grows with every
    trace row ever written, forever, run every `trace_verify_interval_s`)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute("SELECT stream_id FROM og.invariant_trace_watermark")
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def fetch_new_trace_stream_ids(pool: AsyncConnectionPool, *, since: datetime) -> list[str]:
    """Stream ids that appear in `og.trace` for the first time after `since` (a `created_at` cursor,
    advanced by the caller once discovery completes) and are not already known
    (`og.invariant_trace_watermark`) -- bounded by rows written since `since`, not the whole trace
    table."""
    sql = """
        SELECT DISTINCT t.stream_id
        FROM og.trace t
        WHERE t.created_at > %(since)s
          AND NOT EXISTS (
              SELECT 1 FROM og.invariant_trace_watermark w WHERE w.stream_id = t.stream_id
          )
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"since": since})
        rows = await cur.fetchall()
    return [r[0] for r in rows]


async def get_trace_watermark(pool: AsyncConnectionPool, stream_id: str) -> int:
    """The `seq` to resume `TraceStore.verify(stream_id, from_seq=...)` from -- 0 for a stream with no
    row yet (verify from the start)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            "SELECT next_from_seq FROM og.invariant_trace_watermark WHERE stream_id = %(stream_id)s",
            {"stream_id": stream_id},
        )
        row = await cur.fetchone()
    return int(row[0]) if row is not None else 0


async def upsert_trace_watermark(pool: AsyncConnectionPool, stream_id: str, *, next_from_seq: int) -> None:
    """Persist `stream_id`'s resume point as its OWN row (adversarial-review fix: previously every
    stream's position lived in one shared jsonb blob on `og.invariant_check`, rewritten whole on every
    run regardless of how many streams actually advanced)."""
    sql = """
        INSERT INTO og.invariant_trace_watermark (stream_id, next_from_seq, updated_at)
        VALUES (%(stream_id)s, %(next_from_seq)s, now())
        ON CONFLICT (stream_id) DO UPDATE SET
            next_from_seq = EXCLUDED.next_from_seq, updated_at = EXCLUDED.updated_at
    """
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(sql, {"stream_id": stream_id, "next_from_seq": next_from_seq})
        await conn.commit()


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


_UPSERT_VIOLATION_SQL = """
    INSERT INTO og.invariant_violation (check_name, dedupe_key, detected_at, scope, detail)
    VALUES (%(check_name)s, %(dedupe_key)s, %(detected_at)s, %(scope)s, %(detail)s)
    ON CONFLICT (check_name, dedupe_key) DO NOTHING
    RETURNING dedupe_key
"""


async def insert_violations(
    pool: AsyncConnectionPool, check_name: str, violations: list[Violation]
) -> list[Violation]:
    """Upsert each violation into `og.invariant_violation` (the audit trail) keyed on
    `(check_name, dedupe_key)` -- a condition still true on a later run is the SAME row, not appended
    again (adversarial-review fix: the old unkeyed `INSERT` re-counted one K2 over-sale roughly 120x
    across an hour of 60s re-scans). Returns only the violations that were ACTUALLY NEW this call (a
    `RETURNING` on a real insert, nothing on a conflict) -- callers use this, not the full input list,
    to advance Prometheus counters/`total_violations` so a persisting condition is counted once, not
    once per run. A no-op (returns `[]`) for an empty list."""
    if not violations:
        return []
    from psycopg.types.json import Jsonb  # local import: only this write path needs the jsonb adapter

    now = datetime.now(UTC)
    newly_inserted: list[Violation] = []
    async with pool.connection() as conn, conn.cursor() as cur:
        for violation in violations:
            await cur.execute(
                _UPSERT_VIOLATION_SQL,
                {
                    "check_name": check_name,
                    "dedupe_key": violation.dedupe_key,
                    "detected_at": now,
                    "scope": Jsonb(violation.scope),
                    "detail": Jsonb(violation.detail),
                },
            )
            if await cur.fetchone() is not None:
                newly_inserted.append(violation)
        await conn.commit()
    return newly_inserted


_SELECT_SUMMARY_SQL = (
    "SELECT check_name, last_run_at, total_violations, last_violations FROM og.invariant_check"
)


async def read_summary(pool: AsyncConnectionPool) -> InvariantsSummary:
    """The measured read model `api/routers/health.py`/`dispatch.py` expose: a plain read of
    `og.invariant_check`, never a constant. A check that has never run contributes 0 and does not affect
    `as_of` (there is nothing yet to be "as of").

    K1/K2/K13/K13_OUTAGE_GAP are reported as their running TOTAL (`total_violations`) --
    Prometheus-counter-shaped, matching `og_reserve_breaches_total`/`og_double_sold_kwh_total`/
    `og_lock_violations_total`/`og_k13_outage_gap_total`, which "must stay 0" for the life of the
    deployment, not just the latest run. The two orphan checks are reported as the count found in the
    MOST RECENT run (`last_violations`) -- they describe current outstanding bookkeeping debt (a gauge),
    which can legitimately go back down once cleaned up, unlike a breach count.
    """
    from opengrid.invariants.models import (
        CHECK_K1_RESERVE_BREACH,
        CHECK_K2_DOUBLE_SOLD,
        CHECK_K13_LOCK_VIOLATION,
        CHECK_K13_OUTAGE_GAP,
        CHECK_K13_RESTORE_LAG,
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
        outage_gaps=int(totals.get(CHECK_K13_OUTAGE_GAP, 0)),
        restore_lag=int(totals.get(CHECK_K13_RESTORE_LAG, 0)),
        orphan_reservations=last_counts.get(CHECK_ORPHAN_RESERVATION, 0),
        orphan_commitments=last_counts.get(CHECK_ORPHAN_COMMITMENT, 0),
        as_of=min(run_ats) if run_ats else None,
    )
