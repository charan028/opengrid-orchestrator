"""EIA v2 electricity/rto/region-data/data (standby load fallback). Sim base
URL is `/eia` per interfaces/http/market-api.md §2."""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Query, Request
from fastapi.responses import JSONResponse, Response

from ogsim.market.runtime import get_runtime, http_anomaly_gate

router = APIRouter(prefix="/eia")

EIA_MISSING_KEY_STATUS = 403


@router.get("/electricity/rto/region-data/data", response_model=None)
@router.get("/electricity/rto/region-data/data/", response_model=None)
async def region_data(
    request: Request,
    api_key: str | None = Query(default=None),
) -> Response | dict[str, Any]:
    rt = get_runtime(request)
    gated = await http_anomaly_gate(request, "eia")
    if gated is not None:
        return gated
    if api_key != rt.cfg.eia_api_key:
        return JSONResponse(
            status_code=EIA_MISSING_KEY_STATUS, content={"error": "invalid or missing api_key"}
        )
    respondent = "ERCO"
    for key, value in request.query_params.multi_items():
        if key == "facets[respondent][]":
            respondent = value
    return rt.data.eia_response(rt.now(), respondent=respondent)
