"""ERCOT B2C token endpoint + the five Public API data products.

Base URL for the five data products is `/ercot` (interfaces/http/market-api.md
§1's sim base URL `http://127.0.0.1:8090/ercot`); the B2C token endpoint
keeps its real, unprefixed path pattern since it mimics an external Azure
AD B2C URL, not an ERCOT Public API path.
"""

from __future__ import annotations

from typing import Any

from fastapi import APIRouter, Header, Query, Request
from fastapi.responses import JSONResponse, Response

from ogsim.market.data import (
    AS_FIELDS,
    DEFAULT_PAGE_SIZE,
    LOAD_FIELDS,
    SOLAR_FIELDS,
    SPP_FIELDS,
    WIND_FIELDS,
    report_response,
)
from ogsim.market.runtime import MarketRuntime, get_runtime, http_anomaly_gate, key_gate
from ogsim.market.security import TOKEN_LIFETIME_S, key_kind

token_router = APIRouter()
router = APIRouter(prefix="/ercot")

DEFAULT_PAGE = 1
OAUTH_PASSWORD_GRANT = "password"


@token_router.post(
    "/ercotb2c.onmicrosoft.com/ercotb2c.onmicrosoft.com/B2C_1_PUBAPI-ROPC-FLOW/oauth2/v2.0/token",
    response_model=None,
)
@token_router.post("/token", response_model=None)
async def b2c_token(request: Request) -> JSONResponse | dict[str, Any]:
    """Accepts any configured test user (username/password form body,
    grant_type=password) and returns id_token/access_token, 1h expiry."""
    rt = get_runtime(request)
    form = await request.form()
    username = str(form.get("username", ""))
    password = str(form.get("password", ""))
    grant_type = str(form.get("grant_type", ""))
    if grant_type != OAUTH_PASSWORD_GRANT:
        return JSONResponse(status_code=400, content={"error": "unsupported_grant_type"})
    token = rt.tokens.authenticate(username, password)
    if token is None:
        return JSONResponse(
            status_code=400,
            content={"error": "invalid_grant", "error_description": "invalid username or password"},
        )
    return {
        "token_type": "Bearer",
        "id_token": token,
        "access_token": token,
        "expires_in": TOKEN_LIFETIME_S,
        "not_before": int(rt.now().timestamp()),
    }


async def _check_market_auth(
    request: Request, rt: MarketRuntime, product: str, subscription_key: str | None
) -> Response | None:
    """Runs the ERCOT-call gates in order: subscription-key presence, the
    `http_401_primary` anomaly (key rotation test), then bearer-token
    presence. Per interfaces/http/market-api.md §1, the simulator "does not
    need to validate the bearer token's cryptographic content" - any
    non-empty `Authorization` header is accepted. Returns the first failing
    response, or None if all pass."""
    kind = key_kind(rt.cfg, subscription_key)
    if kind is None:
        return JSONResponse(status_code=401, content={"error": "invalid subscription key"})
    key_denied = key_gate(request, product, kind)
    if key_denied is not None:
        return key_denied
    if not request.headers.get("authorization", "").strip():
        return JSONResponse(status_code=401, content={"error": "missing bearer token"})
    return None


@router.get("/np6-905-cd/spp_node_zone_hub", response_model=None)
async def spp_node_zone_hub(
    request: Request,
    settlementPointType: str = Query(default="LZ"),
    settlementPoint: str | None = Query(default=None),
    deliveryDateFrom: str | None = Query(default=None),
    deliveryDateTo: str | None = Query(default=None),
    size: int = Query(default=DEFAULT_PAGE_SIZE),
    page: int = Query(default=DEFAULT_PAGE),
    Ocp_Apim_Subscription_Key: str | None = Header(default=None, alias="Ocp-Apim-Subscription-Key"),
) -> Response | dict[str, Any]:
    rt = get_runtime(request)
    product = "np6-905-cd"
    gated = await http_anomaly_gate(request, product)
    if gated is not None:
        return gated
    auth_err = await _check_market_auth(request, rt, product, Ocp_Apim_Subscription_Key)
    if auth_err is not None:
        return auth_err
    rows = rt.data.spp_rows(
        rt.now(), settlement_point_type=settlementPointType, settlement_point=settlementPoint
    )
    return report_response(SPP_FIELDS, rows, page, size)


@router.get("/np6-345-cd/act_sys_load_by_wzn", response_model=None)
async def act_sys_load_by_wzn(
    request: Request,
    operatingDateFrom: str | None = Query(default=None),
    operatingDateTo: str | None = Query(default=None),
    size: int = Query(default=DEFAULT_PAGE_SIZE),
    page: int = Query(default=DEFAULT_PAGE),
    Ocp_Apim_Subscription_Key: str | None = Header(default=None, alias="Ocp-Apim-Subscription-Key"),
) -> Response | dict[str, Any]:
    rt = get_runtime(request)
    product = "np6-345-cd"
    gated = await http_anomaly_gate(request, product)
    if gated is not None:
        return gated
    auth_err = await _check_market_auth(request, rt, product, Ocp_Apim_Subscription_Key)
    if auth_err is not None:
        return auth_err
    rows = rt.data.load_rows(rt.now())
    return report_response(LOAD_FIELDS, rows, page, size)


@router.get("/np4-732-cd/wpp_hrly_avrg_actl_fcast", response_model=None)
async def wpp_hrly_avrg_actl_fcast(
    request: Request,
    postedDatetimeFrom: str | None = Query(default=None),
    postedDatetimeTo: str | None = Query(default=None),
    size: int = Query(default=DEFAULT_PAGE_SIZE),
    page: int = Query(default=DEFAULT_PAGE),
    Ocp_Apim_Subscription_Key: str | None = Header(default=None, alias="Ocp-Apim-Subscription-Key"),
) -> Response | dict[str, Any]:
    rt = get_runtime(request)
    product = "np4-732-cd"
    gated = await http_anomaly_gate(request, product)
    if gated is not None:
        return gated
    auth_err = await _check_market_auth(request, rt, product, Ocp_Apim_Subscription_Key)
    if auth_err is not None:
        return auth_err
    rows = rt.data.renewable_rows(rt.now(), "wind")
    return report_response(WIND_FIELDS, rows, page, size)


@router.get("/np4-737-cd/spp_hrly_avrg_actl_fcast", response_model=None)
async def spp_hrly_avrg_actl_fcast(
    request: Request,
    postedDatetimeFrom: str | None = Query(default=None),
    postedDatetimeTo: str | None = Query(default=None),
    size: int = Query(default=DEFAULT_PAGE_SIZE),
    page: int = Query(default=DEFAULT_PAGE),
    Ocp_Apim_Subscription_Key: str | None = Header(default=None, alias="Ocp-Apim-Subscription-Key"),
) -> Response | dict[str, Any]:
    rt = get_runtime(request)
    product = "np4-737-cd"
    gated = await http_anomaly_gate(request, product)
    if gated is not None:
        return gated
    auth_err = await _check_market_auth(request, rt, product, Ocp_Apim_Subscription_Key)
    if auth_err is not None:
        return auth_err
    rows = rt.data.renewable_rows(rt.now(), "solar")
    return report_response(SOLAR_FIELDS, rows, page, size)


@router.get("/np4-188-cd/dam_clear_price_for_cap", response_model=None)
async def dam_clear_price_for_cap(
    request: Request,
    deliveryDateFrom: str | None = Query(default=None),
    deliveryDateTo: str | None = Query(default=None),
    size: int = Query(default=DEFAULT_PAGE_SIZE),
    page: int = Query(default=DEFAULT_PAGE),
    Ocp_Apim_Subscription_Key: str | None = Header(default=None, alias="Ocp-Apim-Subscription-Key"),
) -> Response | dict[str, Any]:
    rt = get_runtime(request)
    product = "np4-188-cd"
    gated = await http_anomaly_gate(request, product)
    if gated is not None:
        return gated
    auth_err = await _check_market_auth(request, rt, product, Ocp_Apim_Subscription_Key)
    if auth_err is not None:
        return auth_err
    rows = rt.data.as_rows(rt.now())
    return report_response(AS_FIELDS, rows, page, size)
