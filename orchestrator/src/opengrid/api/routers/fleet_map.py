"""Fleet and customer maps (Gitea #19): every hub with its position, health, activity and the
obligations it serves, and every customer site with its live meter reading.

Read-only; viewer or operator. The hub snapshot is one query, cached for 2 s
(`opengrid.api.views_ext.FleetMapService`), so 2,000 hubs answer well inside 300 ms.
"""

from __future__ import annotations

from collections import Counter
from typing import Annotated, Any

from fastapi import APIRouter, Depends, HTTPException, status
from fastapi.responses import JSONResponse

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.deps import get_config, get_store
from opengrid.api.routers.fleet_search import classify_asset, home_stations, mobile_units, substation_keys
from opengrid.api.views_ext import (
    ACTIVITIES,
    CUSTOMER_SITES_FILE,
    DEFAULT_ACTIVITY_EPSILON_KW,
    ExtViewsProtocol,
    FleetMapService,
    activity_counts,
    build_customer_sites,
    get_ext_views,
    get_fleet_map_service,
    grid_config_dir,
    load_customer_sites,
)
from opengrid.platform.config import Config

router = APIRouter(prefix="/og/api", tags=["maps"])

_DEFAULT_SITE_READING_MAX_AGE_S = 900.0


@router.get("/fleet/map", response_model=None)
async def fleet_map(
    service: Annotated[FleetMapService, Depends(get_fleet_map_service)],
    store: Annotated[Any, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    zone: str | None = None,
    bank: str | None = None,
    activity: str | None = None,
) -> JSONResponse:
    """`{generated_at, count, activity_counts, coord_sources, warnings, hubs[]}`. Each hub: `hub_id,
    bank_id, zone, lat, lon, coord_source, health, activity, kw, soc_kwh, soc_pct, reserve_kwh, rated_kw,
    home_load_kw, meter_kw, pv_kw, fault_code, last_seen_at, serving_obligations[], can_serve_services[],
    asset_class` (HOME|MOBILE|UTILITY_SCALE, `fleet_search.classify_asset`). `depots` are the D-31 home
    stations with their assigned units. `activity_counts` covers the filtered set."""
    if activity is not None and activity not in ACTIVITIES:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, detail=f"activity must be one of {', '.join(ACTIVITIES)}"
        )
    snapshot = await service.snapshot()
    mobile = set(mobile_units())
    substations = await substation_keys(store)
    hubs = [
        {
            **h,
            "asset_class": classify_asset(
                h["hub_id"], h.get("bank_id"), mobile=mobile, substations=substations
            ),
        }
        for h in snapshot.hubs
        if (zone is None or h["zone"] == zone)
        and (bank is None or h["bank_id"] == bank)
        and (activity is None or h["activity"] == activity)
    ]
    # Plain JSON-native values throughout: `JSONResponse` skips `jsonable_encoder`, which dominated the
    # 2,000-hub response time.
    body = {
        "generated_at": snapshot.generated_at.isoformat(),
        "count": len(hubs),
        "activity_counts": activity_counts(hubs),
        "coord_sources": dict(Counter(h["coord_source"] for h in hubs)),
        "warnings": list(service.warnings),
        "hubs": hubs,
        "depots": home_stations(),
    }
    return JSONResponse(body)


@router.get("/customers/map")
async def customers_map(
    views: Annotated[ExtViewsProtocol, Depends(get_ext_views)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
) -> dict[str, Any]:
    """`{count, consuming_count, sites[]}`. Each site: `customer_id, site_id, name, service_type,
    contract_services, lat, lon, coord_source, kw, consuming, reading_ts, reading_quality,
    has_active_contract`. Positions come from `config/grid/customer_sites.json` until the customer seed
    carries them; a metered site missing from that table is returned with `lat`/`lon` null."""
    max_age_s = float(cfg.get("api.customers_map.reading_max_age_s", _DEFAULT_SITE_READING_MAX_AGE_S))
    epsilon_kw = float(cfg.get("api.fleet_map.activity_epsilon_kw", DEFAULT_ACTIVITY_EPSILON_KW))
    configured = load_customer_sites(grid_config_dir(cfg) / CUSTOMER_SITES_FILE)
    readings = await views.customer_site_readings(max_age_s=max_age_s)
    services = await views.customer_contract_services()
    sites = build_customer_sites(configured, readings, services, epsilon_kw=epsilon_kw)
    return {
        "count": len(sites),
        "consuming_count": sum(1 for s in sites if s["consuming"]),
        "sites": sites,
    }
