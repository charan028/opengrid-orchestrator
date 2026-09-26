"""Health screen (02b S7.1, S8 screen 5) and the Control room's first paint (02b S8 screen 1: every
KPI tile there -- fleet MW/MWh, active commitments, net margin, the three invariant counters -- reads
`GET /og/api/health`, per `opengrid.ui.routes.control_room`/`templates/control_room.html`): aggregated
process/feed/hub health, alert list, alert ack, and the deploy-time liveness probe.

Response shape is fixed by what `opengrid.ui` (ui-a) already consumes: `processes` is a dict keyed by
process name (not a list), `feeds`/`alerts` are full lists (not just counts), and the control-room KPIs
(`fleet_mw`, `active_commitments`, `net_margin_usd`, `reserve_breaches`, `double_sold_kwh`,
`commitment_switches`, `as_of`) are top-level fields alongside them.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from psycopg_pool import AsyncConnectionPool
from sse_starlette.sse import EventSourceResponse

from opengrid.api.auth import Identity, require_loopback_health_probe, require_operator, require_viewer
from opengrid.api.deps import get_config, get_store
from opengrid.api.sse import sse_response
from opengrid.api.store import HealthSnapshot, StoreProtocol
from opengrid.core.models.platform import Alert
from opengrid.health.queries import fetch_degraded_modes
from opengrid.invariants import InvariantsSummary, read_summary
from opengrid.platform.config import Config

logger = logging.getLogger(__name__)

router = APIRouter(tags=["health"])

# `commitment_switches` has no measuring check yet (out of opengrid.invariants' scope -- K1/K2/K13 +
# orphan bookkeeping + K11 only, per its task brief); 0 stays the correct value for a healthy run until
# one exists. `reserve_breaches`/`double_sold_kwh` below come from `opengrid.invariants.read_summary`
# (`og.invariant_check`, populated by that package's periodic K1/K2 checks) -- a measurement, not this
# constant.
_UNMEASURED_INVARIANT_COUNTERS = {"commitment_switches": 0}

_UNAVAILABLE_INVARIANTS_SUMMARY = InvariantsSummary.unavailable()


def _get_optional_pool(request: Request) -> AsyncConnectionPool | None:
    """Like `opengrid.api.deps.get_pool`, but tolerant of `app.state.pool` never having been set: this
    router's own unit test suite overrides every OTHER dependency and never runs the real app lifespan
    (`opengrid.api.app._lifespan`) that would set it. A missing pool degrades this endpoint's invariant
    counters to "not yet measured" (`_UNAVAILABLE_INVARIANTS_SUMMARY`) rather than failing the whole
    health payload over an unrelated dependency (K7: degrade, don't trip)."""
    return getattr(request.app.state, "pool", None)


async def _invariants_summary(pool: AsyncConnectionPool | None) -> InvariantsSummary:
    if pool is None:
        return _UNAVAILABLE_INVARIANTS_SUMMARY
    try:
        return await read_summary(pool)
    except Exception:
        logger.warning("opengrid.invariants summary read failed", exc_info=True)
        return _UNAVAILABLE_INVARIANTS_SUMMARY


async def _degraded_modes(pool: AsyncConnectionPool | None) -> list[str]:
    """R2 item 1 (defect fix): `opengrid.health.evaluate_once` already computes the current degraded-mode
    set every cycle (02b S6.5), but only ever returned it in an in-process `HealthSnapshot` inside
    og-settle -- this endpoint built a completely separate `api.store.HealthSnapshot` with no
    degraded-mode field at all, so neither the API response nor the UI could ever show a real value.
    `og.degraded_mode_state` (migration 0020) is `health`'s persisted form of the same computation
    (`health.queries.write_degraded_modes`); this reads it directly rather than re-deriving the modes
    here (BUILD.md S1 "no duplicated functions"). A missing pool or a read failure degrades to an empty
    list (K7: degrade, don't trip), matching `_invariants_summary`'s pattern."""
    if pool is None:
        return []
    try:
        rows = await fetch_degraded_modes(pool)
    except Exception:
        logger.warning("degraded mode read failed", exc_info=True)
        return []
    return sorted(mode for mode, _since in rows)


async def _merge_alert_scopes(pool: AsyncConnectionPool | None, alerts: list[Alert]) -> list[Alert]:
    """R2 item 1: `og.alert.scope_kind`/`scope_ref` (migration 0024) so the UI stops parsing `summary`
    text for which bank/zone/process an alert is about. `api.store`'s own alert queries (`list_alerts`/
    `health_snapshot`/`ack_alert`) don't select these two new columns yet -- rather than wait on that
    api-owned follow-up, this does one direct supplementary read here (same pattern as `_degraded_modes`)
    and merges the values onto the `Alert` objects already fetched. Degrades to the alerts unchanged
    (`scope_kind`/`scope_ref` stay `None`, their model default) on a missing pool or a read failure
    (K7: degrade, don't trip)."""
    ids = [a.id for a in alerts if a.id is not None]
    if pool is None or not ids:
        return alerts
    try:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT id, scope_kind, scope_ref FROM og.alert WHERE id = ANY(%s)", (ids,))
            rows = await cur.fetchall()
    except Exception:
        logger.warning("alert scope read failed", exc_info=True)
        return alerts
    scopes = {row[0]: (row[1], row[2]) for row in rows}
    return [
        alert.model_copy(update={"scope_kind": scopes[alert.id][0], "scope_ref": scopes[alert.id][1]})
        if alert.id in scopes
        else alert
        for alert in alerts
    ]


def _feed_payload(f: Any) -> dict[str, Any]:
    return {
        "source": f.source,
        "product": f.product,
        "last_value_at": f.last_value_at.isoformat() if f.last_value_at else None,
        "quality": "STALE" if f.breaker_open else "GOOD",
        "breaker_open": f.breaker_open,
        "consecutive_failures": f.consecutive_failures,
    }


async def _health_payload(store: StoreProtocol, pool: AsyncConnectionPool | None) -> dict[str, Any]:
    snapshot: HealthSnapshot = await store.health_snapshot(heartbeat_miss_threshold_s=15.0)
    hubs = await store.list_hubs(zone=None, bank_id=None, health=None, limit=2000, offset=0)
    fleet_mw = sum(h["p_kw"] for h in hubs) / 1000.0
    active_commitments = await store.count_active_commitments()
    net_margin_usd = await _todays_net_margin(store)
    invariants = await _invariants_summary(pool)
    degraded_modes = await _degraded_modes(pool)
    scoped_alerts = await _merge_alert_scopes(pool, snapshot.open_alerts)
    return {
        "status": "ok",
        "as_of": datetime.now(UTC).isoformat(),
        "degraded_modes": degraded_modes,
        "processes": {
            p.process: {"pid": p.pid, "ts": p.ts.isoformat(), "status": p.status} for p in snapshot.processes
        },
        "feeds": [_feed_payload(f) for f in snapshot.feeds],
        "hub_health_counts": snapshot.hub_health_counts,
        "alerts": [_alert_payload(a) for a in scoped_alerts],
        "open_alert_count": len(snapshot.open_alerts),
        "fleet_mw": fleet_mw,
        "fleet_mwh": None,  # not yet tracked -- `settle` owns real delivered-MWh accounting (02a S7)
        "active_commitments": active_commitments,
        "net_margin_usd": net_margin_usd,
        "reserve_breaches": invariants.reserve_breaches,
        "double_sold_kwh": invariants.double_sold_kwh,
        "lock_violations": invariants.lock_violations,
        "orphan_reservations": invariants.orphan_reservations,
        "orphan_commitments": invariants.orphan_commitments,
        "invariants_checked_at": invariants.as_of.isoformat() if invariants.as_of else None,
        **_UNMEASURED_INVARIANT_COUNTERS,
    }


async def _todays_net_margin(store: StoreProtocol) -> float | None:
    try:
        rows = await store.profitability_summary(service=None, day=date.today())
    except Exception:
        return None
    if not rows:
        return None
    return float(sum(float(row["net_value"]) for row in rows))


@router.get("/og/api/health", dependencies=[Depends(require_loopback_health_probe)])
async def get_health(
    store: Annotated[StoreProtocol, Depends(get_store)],
    pool: Annotated[AsyncConnectionPool | None, Depends(_get_optional_pool)],
) -> dict[str, Any]:
    """Aggregated health for the deploy poll (`deploy/scripts/deploy.sh`, loopback, no auth header),
    the operator/viewer Health screen, and the Control room's first paint (both reached through
    Apache, also loopback by the time they hit this process -- see `opengrid.api.auth`)."""
    return await _health_payload(store, pool)


@router.get("/og/api/stream/health")
async def stream_health(
    request: Request,
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    # Trailing + defaulted (unlike every other dependency here): `tests/unit/api/test_sse.py` calls this
    # coroutine directly (bypassing FastAPI's dependency injection) with only `store`/`cfg`/`_identity`
    # given by keyword, so `pool` needs a real Python default to remain callable that way.
    pool: Annotated[AsyncConnectionPool | None, Depends(_get_optional_pool)] = None,
) -> EventSourceResponse:
    async def fetch() -> dict[str, Any]:
        return await _health_payload(store, pool)

    return sse_response(request, interval_s=2.0, heartbeat_s=cfg.get("api.sse_heartbeat_s", 15), fetch=fetch)


@router.get("/og/api/stream/alerts")
async def stream_alerts(
    request: Request,
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    # Trailing + defaulted, matching `stream_health`'s own note: callable directly in tests without a
    # real pool.
    pool: Annotated[AsyncConnectionPool | None, Depends(_get_optional_pool)] = None,
) -> EventSourceResponse:
    async def fetch() -> dict[str, Any]:
        alerts = await store.list_alerts(open_only=True)
        scoped_alerts = await _merge_alert_scopes(pool, alerts)
        return {"open_alerts": [_alert_payload(a) for a in scoped_alerts]}

    return sse_response(request, interval_s=2.0, heartbeat_s=cfg.get("api.sse_heartbeat_s", 15), fetch=fetch)


def _alert_payload(alert: Alert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "rule": alert.rule,
        "severity": alert.severity,
        "summary": alert.summary,
        "opened_at": alert.opened_at.isoformat(),
        "acked_by": alert.acked_by,
        # R2 item 1 (migration 0024): structured scope so the UI stops parsing `summary` text. `None`
        # for an alert this payload didn't merge scope onto (e.g. `stream_alerts`, unscoped alerts).
        "scope_kind": alert.scope_kind,
        "scope_ref": alert.scope_ref,
    }


@router.post("/og/api/alerts/{alert_id}/ack")
async def ack_alert(
    alert_id: int,
    store: Annotated[StoreProtocol, Depends(get_store)],
    identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    """Screens 1/5's "Ack an alert" action (02b S8) -- acknowledgement only, does not clear the alert
    (clearing is `health`'s job when the underlying condition resolves)."""
    alert = await store.ack_alert(alert_id, operator=identity.user)
    if alert is None:
        raise HTTPException(status.HTTP_404_NOT_FOUND, detail="alert not found")
    return _alert_payload(alert)
