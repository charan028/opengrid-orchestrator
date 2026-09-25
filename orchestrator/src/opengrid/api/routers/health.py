"""Health screen (02b S7.1, S8 screen 5): aggregated process/feed/hub health, alert list, alert ack,
and the deploy-time liveness probe.
"""

from __future__ import annotations

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


def _snapshot_payload(snapshot: HealthSnapshot) -> dict[str, Any]:
    return {
        "processes": [
            {"process": p.process, "pid": p.pid, "ts": p.ts.isoformat(), "status": p.status}
            for p in snapshot.processes
        ],
        "feeds": [
            {
                "source": f.source,
                "product": f.product,
                "last_value_at": f.last_value_at.isoformat() if f.last_value_at else None,
                "breaker_open": f.breaker_open,
                "consecutive_failures": f.consecutive_failures,
            }
            for f in snapshot.feeds
        ],
        "hub_health_counts": snapshot.hub_health_counts,
        "open_alert_count": len(snapshot.open_alerts),
    }


@router.get("/og/api/health", dependencies=[Depends(require_loopback_health_probe)])
async def get_health(store: Annotated[StoreProtocol, Depends(get_store)]) -> dict[str, Any]:
    """Aggregated health for the deploy poll (`deploy/scripts/deploy.sh`, loopback, no auth header)
    and for the operator/viewer Health screen (reached through Apache, also loopback by the time it
    hits this process -- see `opengrid.api.auth`)."""
    snapshot = await store.health_snapshot(heartbeat_miss_threshold_s=15.0)
    return {"status": "ok", **_snapshot_payload(snapshot)}


@router.get("/og/api/stream/health")
async def stream_health(
    request: Request,
    store: Annotated[StoreProtocol, Depends(get_store)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> EventSourceResponse:
    async def fetch() -> dict[str, Any]:
        snapshot = await store.health_snapshot(heartbeat_miss_threshold_s=15.0)
        return _snapshot_payload(snapshot)

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
