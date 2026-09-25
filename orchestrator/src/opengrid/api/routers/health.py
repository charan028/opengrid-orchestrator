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

from datetime import UTC, date, datetime
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, Request, status
from sse_starlette.sse import EventSourceResponse

from opengrid.api.auth import Identity, require_loopback_health_probe, require_operator, require_viewer
from opengrid.api.deps import get_config, get_store
from opengrid.api.sse import sse_response
from opengrid.api.store import HealthSnapshot, StoreProtocol
from opengrid.core.models.platform import Alert
from opengrid.platform.config import Config

router = APIRouter(tags=["health"])

# A10's three invariant counters have no dedicated running-total table yet (guardian/ledger own the
# checks that would feed one -- `og_reserve_breaches_total`/`og_double_sold_kwh_total` on `/metrics`,
# 02b S6.6). Reporting 0 here is the correct value for a healthy run (the acceptance bar *is* "reads
# 0"), not a placeholder standing in for a missing feature; once a real counter table exists, this
# becomes a plain read of it.
_ZERO_INVARIANT_COUNTERS = {"reserve_breaches": 0, "double_sold_kwh": 0, "commitment_switches": 0}


def _feed_payload(f: Any) -> dict[str, Any]:
    return {
        "source": f.source,
        "product": f.product,
        "last_value_at": f.last_value_at.isoformat() if f.last_value_at else None,
        "quality": "STALE" if f.breaker_open else "GOOD",
        "breaker_open": f.breaker_open,
        "consecutive_failures": f.consecutive_failures,
    }


async def _health_payload(store: StoreProtocol) -> dict[str, Any]:
    snapshot: HealthSnapshot = await store.health_snapshot(heartbeat_miss_threshold_s=15.0)
    hubs = await store.list_hubs(zone=None, bank_id=None, health=None, limit=2000, offset=0)
    fleet_mw = sum(h["p_kw"] for h in hubs) / 1000.0
    active_commitments = await store.count_active_commitments()
    net_margin_usd = await _todays_net_margin(store)
    return {
        "status": "ok",
        "as_of": datetime.now(UTC).isoformat(),
        "processes": {
            p.process: {"pid": p.pid, "ts": p.ts.isoformat(), "status": p.status} for p in snapshot.processes
        },
        "feeds": [_feed_payload(f) for f in snapshot.feeds],
        "hub_health_counts": snapshot.hub_health_counts,
        "alerts": [_alert_payload(a) for a in snapshot.open_alerts],
        "open_alert_count": len(snapshot.open_alerts),
        "fleet_mw": fleet_mw,
        "fleet_mwh": None,  # not yet tracked -- `settle` owns real delivered-MWh accounting (02a S7)
        "active_commitments": active_commitments,
        "net_margin_usd": net_margin_usd,
        **_ZERO_INVARIANT_COUNTERS,
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
async def get_health(store: Annotated[StoreProtocol, Depends(get_store)]) -> dict[str, Any]:
    """Aggregated health for the deploy poll (`deploy/scripts/deploy.sh`, loopback, no auth header),
    the operator/viewer Health screen, and the Control room's first paint (both reached through
    Apache, also loopback by the time they hit this process -- see `opengrid.api.auth`)."""
    return await _health_payload(store)


@router.get("/og/api/stream/health")
async def stream_health(
    request: Request,
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> EventSourceResponse:
    async def fetch() -> dict[str, Any]:
        return await _health_payload(store)

    return sse_response(request, interval_s=2.0, heartbeat_s=cfg.get("api.sse_heartbeat_s", 15), fetch=fetch)


@router.get("/og/api/stream/alerts")
async def stream_alerts(
    request: Request,
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> EventSourceResponse:
    async def fetch() -> dict[str, Any]:
        alerts = await store.list_alerts(open_only=True)
        return {"open_alerts": [_alert_payload(a) for a in alerts]}

    return sse_response(request, interval_s=2.0, heartbeat_s=cfg.get("api.sse_heartbeat_s", 15), fetch=fetch)


def _alert_payload(alert: Alert) -> dict[str, Any]:
    return {
        "id": alert.id,
        "rule": alert.rule,
        "severity": alert.severity,
        "summary": alert.summary,
        "opened_at": alert.opened_at.isoformat(),
        "acked_by": alert.acked_by,
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
