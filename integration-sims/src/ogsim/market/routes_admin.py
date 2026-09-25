"""Internal admin API used by ogsim.control to inject/cancel/list market
anomalies. Not part of the simulated ERCOT/EIA/NWS surface."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import JSONResponse
from pydantic import BaseModel

from ogsim.market.anomalies import MARKET_ANOMALY_TYPES, Anomaly
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
    rt.anomalies.sweep_expired()
    return {"ok": True, "anomaly": anomaly.to_dict()}


@router.delete("/anomalies/{anomaly_id}")
async def cancel_anomaly(request: Request, anomaly_id: str) -> dict[str, bool]:
    rt = get_runtime(request)
    found = rt.anomalies.cancel(anomaly_id)
    return {"ok": found}


@router.get("/healthz")
async def healthz(request: Request) -> dict[str, Any]:
    rt = get_runtime(request)
    return {"ok": True, "data_mode": rt.cfg.data_mode, "replay_available": rt.data.use_replay}
