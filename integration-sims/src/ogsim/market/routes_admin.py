"""Internal admin API used by ogsim.control to inject/cancel/list market
anomalies. Not part of the simulated ERCOT/EIA/NWS surface."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ogsim.market.anomalies import MARKET_ANOMALY_TYPES, Anomaly
from ogsim.market.as_dispatch import AS_DISPATCH_TYPES
from ogsim.market.data import active_as_deployment
from ogsim.market.runtime import get_runtime

router = APIRouter(prefix="/admin")

DEFAULT_ANOMALY_DURATION_S = 60.0


class InjectRequest(BaseModel):
    id: str
    type: str
    target: str = "*"
    params: dict[str, Any] = {}
    start: float | None = None
    duration: float = DEFAULT_ANOMALY_DURATION_S


@router.get("/anomalies")
async def list_anomalies(request: Request) -> dict[str, Any]:
    rt = get_runtime(request)
    return {"anomalies": [a.to_dict() for a in rt.anomalies.all()]}


@router.get("/as_deployment")
async def as_deployment(request: Request) -> dict[str, Any]:
    """Build phase, 2026-09-26 (FLEET-SIM): the currently-active simulated ERCOT AS deployment (if
    any), for DISPATCH/the release manager to poll and treat exactly like an operator-declared
    `og.as_deployment` row -- see the FLEET-SIM build report's wiring note. `{"active": None}` means
    nothing is currently declared."""
    rt = get_runtime(request)
    return {"active": active_as_deployment(rt.anomalies, rt.now())}


@router.post("/anomalies", response_model=None)
async def inject_anomaly(request: Request, body: InjectRequest) -> JSONResponse | dict[str, Any]:
    rt = get_runtime(request)
    if body.type not in MARKET_ANOMALY_TYPES:
        return JSONResponse(
            status_code=422,
            content={
                "error": f"unknown market anomaly type '{body.type}'",
                "known": sorted(MARKET_ANOMALY_TYPES),
            },
        )
    start = body.start if body.start is not None else rt.now().timestamp()
    anomaly = Anomaly(
        id=body.id,
        type=body.type,
        target=body.target,
        params=body.params,
        start=start,
        duration=body.duration,
    )
    rt.anomalies.inject(anomaly)
    published: list[str] = []
    if body.type in AS_DISPATCH_TYPES:
        # One-shot: ERCOT issues the instruction(s) now; the anomaly window only records it.
        published = rt.as_dispatch.apply(body.type, body.target, body.params, rt.now())
    # Bug fix (build phase, 2026-09-26): must sweep against the injected/fake clock (`rt.now()`), not
    # `sweep_expired`'s own real-wall-clock default -- once the real system clock drifted past a fake
    # clock's injected "now" (as it does for any test whose fake clock is set to a near-future time,
    # e.g. later the same day), the real clock could already be past `start + duration +
    # EXPIRY_GRACE_PERIOD_S`, sweeping an anomaly the instant it was injected, before any caller ever
    # observed it as active.
    rt.anomalies.sweep_expired(rt.now().timestamp())
    result: dict[str, Any] = {"ok": True, "anomaly": anomaly.to_dict()}
    if published:
        result["instructions"] = published
    return result


@router.delete("/anomalies/{anomaly_id}")
async def cancel_anomaly(request: Request, anomaly_id: str) -> dict[str, bool]:
    rt = get_runtime(request)
    found = rt.anomalies.cancel(anomaly_id)
    return {"ok": found}


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    rt = get_runtime(request)
    return {"ok": True, "data_mode": rt.cfg.data_mode, "replay_available": rt.data.use_replay}
