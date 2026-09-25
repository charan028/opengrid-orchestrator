"""NWS api.weather.gov points + hourly gridpoint forecast. Sim base URL is
`/nws` per interfaces/http/market-api.md §3."""

from __future__ import annotations

from datetime import datetime
from email.utils import parsedate_to_datetime
from typing import Any

from fastapi import APIRouter, Header, Request
from fastapi.responses import JSONResponse, Response

from ogsim.market.runtime import get_runtime, http_anomaly_gate

router = APIRouter(prefix="/nws")

HTTP_NOT_MODIFIED = 304


def _require_user_agent(user_agent: str | None) -> JSONResponse | None:
    if not user_agent:
        return JSONResponse(status_code=400, content={"error": "User-Agent header is required"})
    return None


def _not_modified(if_modified_since: str | None, updated_at_iso: str) -> bool:
    """True if the client's `If-Modified-Since` is at or after `updated`, per
    market-api.md §3's "matching If-Modified-Since" -> 304 behaviour."""
    if not if_modified_since:
        return False
    try:
        client_time = parsedate_to_datetime(if_modified_since)
        updated_at = datetime.fromisoformat(updated_at_iso)
    except (ValueError, TypeError):
        return False
    return client_time >= updated_at


@router.get("/points/{lat_lon}", response_model=None)
async def points(
    request: Request, lat_lon: str, user_agent: str | None = Header(default=None)
) -> Response | dict[str, Any]:
    rt = get_runtime(request)
    err = _require_user_agent(user_agent)
    if err is not None:
        return err
    gated = await http_anomaly_gate(request, "nws")
    if gated is not None:
        return gated
    try:
        lat_s, lon_s = lat_lon.split(",")
        lat, lon = float(lat_s), float(lon_s)
    except ValueError:
        return JSONResponse(status_code=400, content={"error": "expected {lat},{lon}"})
    return rt.data.nws_points(lat, lon)


@router.get("/gridpoints/{office}/{x},{y}/forecast/hourly", response_model=None)
async def gridpoint_forecast_hourly(
    request: Request,
    office: str,
    x: int,
    y: int,
    user_agent: str | None = Header(default=None),
    if_modified_since: str | None = Header(default=None),
) -> Response | dict[str, Any]:
    rt = get_runtime(request)
    err = _require_user_agent(user_agent)
    if err is not None:
        return err
    gated = await http_anomaly_gate(request, "nws")
    if gated is not None:
        return gated
    updated_at = rt.data.nws_updated_at(rt.now())
    if _not_modified(if_modified_since, updated_at.isoformat()):
        return Response(status_code=HTTP_NOT_MODIFIED)
    return rt.data.nws_hourly_forecast(rt.now(), office, x, y)
