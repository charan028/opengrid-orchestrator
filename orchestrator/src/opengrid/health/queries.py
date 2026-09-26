"""Database I/O for the health evaluator (02b S6.4). Isolated from `health.rules`'s pure logic per
BUILD.md S5a ("pure logic separated from I/O")."""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from psycopg import sql
from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.platform import Alert, FeedStatus, Heartbeat
from opengrid.health.model import AlertFinding

_FETCH_HEARTBEATS_SQL = "SELECT process, pid, ts, status FROM og.heartbeat"

_FETCH_HUB_STATES_SQL = """
SELECT h.zone, hs.hub_id, hs.last_seen_at, hs.fault_code, hs.health
FROM og.hub_state hs JOIN og.hub h ON h.hub_id = hs.hub_id
"""

# Hub health is soft state re-derived from scratch every cycle (like fleet's telemetry/hub_state
# flush, opengrid.fleet.pg_backend): its transaction commits asynchronously (WAL still written, just
# not fsync-waited), so `write_hub_health_batch`'s single statement per chunk does not queue behind the
# engine's own writes waiting on the ~0.5s WAL fsync observed on the base server.
_ASYNC_COMMIT_SQL = "SET LOCAL synchronous_commit TO OFF"
_WRITE_HUB_HEALTH_CHUNK_SIZE = 500

_FETCH_FEED_STATUSES_SQL = """
SELECT source, product, last_value_at, last_success_at, consecutive_failures, breaker_open, active_key
FROM og.feed_status
"""

_FETCH_BANK_LOADS_SQL = """
SELECT b.bank_id, b.kva_rating,
       (SELECT fo.value FROM og.feed_obs fo
        WHERE fo.source = 'scada' AND fo.product = b.bank_id AND fo.series = 'APPARENT_POWER_KVA'
        ORDER BY fo.ts DESC LIMIT 1) AS load_kva
FROM og.bank b
"""

_FETCH_LATEST_HUB_SEEN_AT_SQL = "SELECT MAX(last_seen_at) FROM og.hub_state"
_FETCH_LATEST_SCADA_OBS_AT_SQL = "SELECT MAX(ts) FROM og.feed_obs WHERE source = 'scada'"
# `og.feed_obs.product` holds the bank_id for SCADA rows (see `_FETCH_BANK_LOADS_SQL`'s join above) --
# ALR-SCADA-SILENT-BANK (R3 review fix) needs the freshest reading PER bank, not just the fleet-wide max.
_FETCH_LATEST_SCADA_OBS_BY_BANK_SQL = """
SELECT product AS bank_id, MAX(ts) AS latest_seen_at FROM og.feed_obs WHERE source = 'scada' GROUP BY product
"""

_FETCH_DEGRADED_MODES_SQL = "SELECT mode, since FROM og.degraded_mode_state"
_INSERT_DEGRADED_MODE_SQL = """
INSERT INTO og.degraded_mode_state (mode, since) VALUES (%(mode)s, %(since)s)
ON CONFLICT (mode) DO NOTHING
"""
_DELETE_DEGRADED_MODE_SQL = "DELETE FROM og.degraded_mode_state WHERE mode = %(mode)s"

_FETCH_OPEN_ALERTS_SQL = """
SELECT id, rule, severity, summary, detail, opened_at, cleared_at, acked_by, scope_kind, scope_ref
FROM og.alert WHERE cleared_at IS NULL
"""

# Structured scope columns (migration 0024): populated from `AlertFinding.detail`'s "scope_kind"/
# "scope_ref" keys when present, additive alongside `detail` itself (unchanged). This is the single
# `og.alert` writer (`opengrid.guardian.repo.PgAlertPort.raise_alert` calls this same function), so
# guardian's ALR-SCOPE-CONSERVATIVE/ALR-SAFE-STOP-REQUESTED alerts -- whose `detail` already carries
# "scope_kind"/"scope_ref" (`opengrid.guardian.main.apply_escalation`) -- get these columns with no
# guardian-side change needed.
_INSERT_ALERT_SQL = """
INSERT INTO og.alert (rule, severity, summary, detail, opened_at, scope_kind, scope_ref)
VALUES (%(rule)s, %(severity)s, %(summary)s, %(detail)s, %(opened_at)s, %(scope_kind)s, %(scope_ref)s)
RETURNING id
"""

_CLEAR_ALERT_SQL = "UPDATE og.alert SET cleared_at = %(cleared_at)s WHERE id = %(alert_id)s"


async def fetch_heartbeats(pool: AsyncConnectionPool) -> list[Heartbeat]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_HEARTBEATS_SQL)
        rows = await cur.fetchall()
    return [Heartbeat(process=r[0], pid=r[1], ts=r[2], status=r[3]) for r in rows]


async def fetch_hub_states(
    pool: AsyncConnectionPool,
) -> list[tuple[str, str, datetime | None, str | None, str]]:
    """Returns `(zone, hub_id, last_seen_at, fault_code, current_health)` tuples; `current_health` is the
    classification already stored on `hub_state.health` from the previous cycle, so callers can skip
    writing rows whose classification hasn't changed (dispatch-live pass: ~2,000 hubs/cycle, most of
    which don't flip state cycle-to-cycle)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_HUB_STATES_SQL)
        rows = await cur.fetchall()
    return [(r[0], r[1], r[2], r[3], r[4]) for r in rows]


async def write_hub_health_batch(pool: AsyncConnectionPool, changes: list[tuple[str, str]]) -> None:
    """Write `(hub_id, health)` classification changes in one batched `UPDATE ... FROM (VALUES ...)`
    statement per chunk, under asynchronous commit (see `_ASYNC_COMMIT_SQL`) -- not one single-row
    `UPDATE`+commit per hub (merge task, dispatch-live pass: `og-settle`'s health evaluator did ~2,000
    single-row commits/cycle to this soft-state column, contending with the engine's own writes for the
    ~0.5s WAL fsync observed on the base server). Chunked at `_WRITE_HUB_HEALTH_CHUNK_SIZE` rows/statement
    for the same reason `fleet.pg_backend.upsert_hub_states` chunks: staying under Postgres's
    parameter-count ceiling as `hub_count` grows past MVP-S's ~2,000.

    Callers pass only hubs whose classification actually changed; an empty list is a no-op (no round trip
    for a cycle where nothing flipped state).
    """
    if not changes:
        return
    row_placeholder = sql.SQL("(%s, %s)")
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_ASYNC_COMMIT_SQL)
        for start in range(0, len(changes), _WRITE_HUB_HEALTH_CHUNK_SIZE):
            chunk = changes[start : start + _WRITE_HUB_HEALTH_CHUNK_SIZE]
            values_sql = sql.SQL(", ").join([row_placeholder] * len(chunk))
            statement = sql.SQL(
                "UPDATE og.hub_state AS hs SET health = v.health "
                "FROM (VALUES {values}) AS v(hub_id, health) "
                "WHERE hs.hub_id = v.hub_id"
            ).format(values=values_sql)
            params: list[str] = [value for hub_id, health in chunk for value in (hub_id, health)]
            await cur.execute(statement, params)
        await conn.commit()


async def fetch_feed_statuses(pool: AsyncConnectionPool) -> list[FeedStatus]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_FEED_STATUSES_SQL)
        rows = await cur.fetchall()
    return [
        FeedStatus(
            source=r[0],
            product=r[1],
            last_value_at=r[2],
            last_success_at=r[3],
            consecutive_failures=r[4],
            breaker_open=r[5],
            active_key=r[6],
        )
        for r in rows
    ]


async def fetch_bank_loads(pool: AsyncConnectionPool) -> list[tuple[str, float, float | None]]:
    """Returns `(bank_id, kva_rating, load_kva)` for every bank; `load_kva` is `None` if no SCADA
    reading has ever arrived for it (`ALR-SCADA-OVERLOAD`'s "no reading yet is not an overload" case)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_BANK_LOADS_SQL)
        rows = await cur.fetchall()
    return [(r[0], float(r[1]), float(r[2]) if r[2] is not None else None) for r in rows]


async def fetch_latest_hub_seen_at(pool: AsyncConnectionPool) -> datetime | None:
    """Freshest `og.hub_state.last_seen_at` across every hub -- `ogsim.fleet`'s own MQTT-driven write,
    used as one of `ALR-SIM-OFFLINE`'s two liveness signals for the integration simulators (which write
    no `og.heartbeat` row, see `opengrid.health.model.ALL_PROCESSES`'s docstring). `None` if there are no
    hubs at all (a cold start, not evidence of an offline sim)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_LATEST_HUB_SEEN_AT_SQL)
        row = await cur.fetchone()
    return row[0] if row is not None else None


async def fetch_latest_scada_obs_at(pool: AsyncConnectionPool) -> datetime | None:
    """Freshest SCADA reading timestamp across every bank -- `ogsim.scada`'s own MQTT-driven write, the
    other of `ALR-SIM-OFFLINE`'s two liveness signals. `None` if no SCADA reading has ever arrived (a cold
    start, not evidence of an offline sim)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_LATEST_SCADA_OBS_AT_SQL)
        row = await cur.fetchone()
    return row[0] if row is not None else None


async def fetch_latest_scada_obs_by_bank(pool: AsyncConnectionPool) -> list[tuple[str, datetime]]:
    """`(bank_id, latest_seen_at)` for every bank that has ever had a SCADA reading -- `ALR-SCADA-SILENT-
    BANK`'s (R3 review fix) per-bank counterpart to `fetch_latest_scada_obs_at`'s fleet-wide max. A bank
    with no reading at all simply doesn't appear (cold start, not evidence of that bank's SCADA being
    silent -- same rule as the fleet-wide check)."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_LATEST_SCADA_OBS_BY_BANK_SQL)
        rows = await cur.fetchall()
    return [(r[0], r[1]) for r in rows]


async def fetch_degraded_modes(pool: AsyncConnectionPool) -> list[tuple[str, datetime]]:
    """Returns `(mode, since)` for every currently-active degraded mode (`og.degraded_mode_state`,
    migration 0020) -- the persisted form of `opengrid.health.rules.derive_degraded_modes`'s output, read
    by `opengrid.api.routers.health` for `GET /og/api/health` and by the UI's degraded-mode banners."""
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_DEGRADED_MODES_SQL)
        rows = await cur.fetchall()
    return [(r[0], r[1]) for r in rows]


async def write_degraded_modes(
    pool: AsyncConnectionPool, active_modes: frozenset[str], *, now: datetime
) -> None:
    """Persists the currently-active degraded-mode set (defect fix: previously computed only in-process
    inside og-settle every cycle and never exposed to `opengrid.api`/the UI). Inserts a fresh row
    (`since=now`) for each newly-active mode and deletes rows for modes no longer active; a mode that
    stays active across cycles keeps its original `since`. There are at most 4 possible modes
    (`opengrid.health.model.DegradedMode`), so this is a handful of tiny statements per cycle -- not a
    batching concern like `write_hub_health_batch`'s ~2,000 hubs. Async commit (`_ASYNC_COMMIT_SQL`),
    matching the rest of this module's soft-state writes; a no-op (no round trip) when nothing changed.
    """
    current = {mode for mode, _since in await fetch_degraded_modes(pool)}
    to_insert = active_modes - current
    to_delete = current - active_modes
    if not to_insert and not to_delete:
        return
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_ASYNC_COMMIT_SQL)
        for mode in to_insert:
            await cur.execute(_INSERT_DEGRADED_MODE_SQL, {"mode": mode, "since": now})
        for mode in to_delete:
            await cur.execute(_DELETE_DEGRADED_MODE_SQL, {"mode": mode})
        await conn.commit()


async def fetch_open_alerts(pool: AsyncConnectionPool) -> list[Alert]:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_FETCH_OPEN_ALERTS_SQL)
        rows = await cur.fetchall()
    return [
        Alert(
            id=r[0],
            rule=r[1],
            severity=r[2],
            summary=r[3],
            detail=r[4],
            opened_at=r[5],
            cleared_at=r[6],
            acked_by=r[7],
            scope_kind=r[8],
            scope_ref=r[9],
        )
        for r in rows
    ]


async def raise_alert(pool: AsyncConnectionPool, finding: AlertFinding, *, opened_at: datetime) -> int:
    from psycopg.types.json import Jsonb  # local import: only this write path needs the jsonb adapter

    detail = finding.detail or {}
    scope_kind = detail.get("scope_kind")
    scope_ref = detail.get("scope_ref")
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _INSERT_ALERT_SQL,
            {
                "rule": finding.rule,
                "severity": finding.severity,
                "summary": finding.summary,
                "detail": Jsonb(finding.detail) if finding.detail else None,
                "opened_at": opened_at,
                "scope_kind": scope_kind,
                "scope_ref": scope_ref,
            },
        )
        row = await cur.fetchone()
        await conn.commit()
    if row is None:
        raise RuntimeError("INSERT ... RETURNING id yielded no row")
    return int(row[0])


async def clear_alert(
    pool: AsyncConnectionPool, alert_id: int, *, cleared_at: datetime | None = None
) -> None:
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(
            _CLEAR_ALERT_SQL, {"alert_id": alert_id, "cleared_at": cleared_at or datetime.now(UTC)}
        )
        await conn.commit()


def condition_key_for(alert: Alert) -> str:
    """Rebuild the `condition_key` an open `og.alert` row corresponds to, from its stored detail, so a
    fresh evaluation cycle's findings can be matched against it (rule+scope, mirroring `AlertFinding`)."""
    detail: dict[str, Any] = alert.detail or {}
    scope = (
        detail.get("process")
        or detail.get("zone")
        or detail.get("bank_id")
        or ":".join(str(v) for v in (detail.get("source"), detail.get("product")) if v)
    )
    return f"{alert.rule}:{scope}" if scope else alert.rule
