"""Grid map layers (Gitea #19): the real public grid data the concept page uses
(`config/grid/grid_map_data.json`, see `config/grid/README.md` for provenance and licence), with live
overlays: zone load from `og.feed_obs`, and demand heat cells whose served kW is the fleet's granted
delivery this cycle.

Read-only; viewer or operator. The static file is parsed once per process; the ~7k transmission
polylines are pre-serialised per kV so a full response is a string join, not a 10 MB `json.dumps`.
"""

from __future__ import annotations

import asyncio
import json
from dataclasses import dataclass
from typing import Annotated, Any, Final

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.deps import get_config
from opengrid.api.views_ext import (
    GRID_MAP_DATA_FILE,
    ZONE_CENTROIDS_FILE,
    ExtViewsProtocol,
    FleetMapService,
    GridStatic,
    build_heat_cells,
    build_zone_load,
    get_ext_views,
    get_fleet_map_service,
    grid_config_dir,
    load_grid_static,
    load_zone_centroids,
    weather_zones_by_load_zone,
)
from opengrid.platform.config import Config

router = APIRouter(prefix="/og/api/grid", tags=["grid"])

LAYERS: Final = (
    "zones",
    "weather_zones",
    "utility_batteries",
    "transmission_lines",
    "grid_connection_points",
    "heat_cells",
)
_LINES_PLACEHOLDER: Final = "__OG_TRANSMISSION_LINES__"
_ATTRIBUTION: Final = (
    "Transmission lines: Esri Living Atlas / HIFLD. Zone load: ERCOT public data. Storage sites: HIFLD/EIA. "
    "Residential centroid: LBNL Tracking the Sun."
)


@dataclass(frozen=True, slots=True)
class _PreparedGrid:
    static: GridStatic
    #: kV -> the JSON array text of that kV's polylines.
    lines_json_by_kv: dict[int, str]
    storage: list[dict[str, Any]]
    connection_points: list[dict[str, Any]]


def _prepare(static: GridStatic) -> _PreparedGrid:
    storage = [
        {
            "name": s.get("name"),
            "county": s.get("county"),
            "mw": s.get("mw"),
            "lat": s.get("lat"),
            "lon": s.get("lon"),
            "source": "HIFLD/EIA",
        }
        for s in static.storage
    ]
    points: list[dict[str, Any]] = []
    if static.grid_entry_point:
        points.append({"kind": "grid_entry_point", **static.grid_entry_point})
    if static.residential_centroid:
        points.append({"kind": "residential_centroid", **static.residential_centroid})
    for corridor in static.corridors:
        chokepoint = corridor.get("chokepoint") or [None, None]
        points.append(
            {
                "kind": "corridor_chokepoint",
                "zone_key": corridor.get("zone_key"),
                "zone": corridor.get("zone"),
                "hub": corridor.get("hub"),
                "lat": chokepoint[0],
                "lon": chokepoint[1],
                "bottleneck_kv": corridor.get("bottleneck_kv"),
                "loss_pct": corridor.get("loss_pct"),
                "min_redundancy": corridor.get("min_redundancy"),
                "distance_km": corridor.get("distance_km"),
            }
        )
    return _PreparedGrid(
        static=static,
        lines_json_by_kv={
            kv: json.dumps(lines, separators=(",", ":")) for kv, lines in static.lines_by_kv.items()
        },
        storage=storage,
        connection_points=points,
    )


async def _prepared_grid(request: Request, cfg: Config) -> _PreparedGrid:
    prepared: _PreparedGrid | None = getattr(request.app.state, "grid_static", None)
    if prepared is None:
        path = grid_config_dir(cfg) / GRID_MAP_DATA_FILE
        try:
            static = await asyncio.to_thread(load_grid_static, path)
        except (OSError, ValueError) as exc:
            raise HTTPException(
                status.HTTP_503_SERVICE_UNAVAILABLE, detail=f"grid reference data unavailable: {exc}"
            ) from exc
        prepared = await asyncio.to_thread(_prepare, static)
        request.app.state.grid_static = prepared
    return prepared


def _requested_layers(layers: str | None) -> list[str]:
    if not layers:
        return list(LAYERS)
    requested = [name.strip() for name in layers.split(",") if name.strip()]
    unknown = sorted(set(requested) - set(LAYERS))
    if unknown:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail=f"unknown layer(s) {', '.join(unknown)}; known: {', '.join(LAYERS)}",
        )
    return requested


def _lines_json(prepared: _PreparedGrid, min_kv: int) -> str:
    parts = [f'"{kv}":{text}' for kv, text in prepared.lines_json_by_kv.items() if kv >= min_kv]
    return "{" + ",".join(parts) + "}"


@router.get("/layers", response_model=None)
async def grid_layers(
    request: Request,
    views: Annotated[ExtViewsProtocol, Depends(get_ext_views)],
    fleet_map: Annotated[FleetMapService, Depends(get_fleet_map_service)],
    cfg: Annotated[Config, Depends(get_config)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    layers: Annotated[str | None, Query(description="comma-separated subset of the layer names")] = None,
    min_kv: Annotated[int, Query(ge=0, description="drop transmission lines below this voltage")] = 0,
) -> Response:
    """`{source, layers: [...], zones, weather_zones, utility_batteries, transmission_lines_by_kv,
    transmission_line_counts, grid_connection_points, heat_cells}` (only the requested layers).

    - `zones`: per load zone, `load_mw` = the sum of its mapped ERCOT weather zones' latest live load
      (`live=false` falls back to the export's snapshot); `approximate_mapping` flags the LZ -> weather-
      zone correspondence as approximate (see `opengrid.forecast.service`).
    - `transmission_lines_by_kv`: `{"345": [{path: [[lon, lat], ...], owner, status}, ...], ...}`.
    - `heat_cells`: per bank (SCADA `REAL_POWER_KW` demand) and per zone (ERCOT load), with
      `served_kw` = the fleet's granted delivery there this cycle and `unserved_kw` = demand - served.
    """
    requested = _requested_layers(layers)
    prepared = await _prepared_grid(request, cfg)
    static = prepared.static
    body: dict[str, Any] = {
        "source": {
            "file": GRID_MAP_DATA_FILE,
            "sha256": static.sha256,
            "note": static.note,
            "attribution": _ATTRIBUTION,
        },
        "layers": requested,
    }
    zones: list[dict[str, Any]] = []
    if {"zones", "weather_zones", "heat_cells"} & set(requested):
        snapshot = await fleet_map.snapshot()
        weather_live = await views.weather_zone_load_mw()
        centroids = load_zone_centroids(grid_config_dir(cfg) / ZONE_CENTROIDS_FILE)
        fleet_zones = {h["zone"] for h in snapshot.hubs}
        zones, weather = build_zone_load(
            weather_live, static, weather_zones_by_load_zone(cfg), centroids, fleet_zones
        )
        if "zones" in requested:
            body["zones"] = zones
        if "weather_zones" in requested:
            body["weather_zones"] = weather
        if "heat_cells" in requested:
            bank_scada = await views.bank_scada_load_kw()
            body["heat_cells"] = build_heat_cells(snapshot.hubs, bank_scada, zones, snapshot.bank_granted_kw)
    if "utility_batteries" in requested:
        body["utility_batteries"] = prepared.storage
    if "grid_connection_points" in requested:
        body["grid_connection_points"] = prepared.connection_points
    if "transmission_lines" in requested:
        body["transmission_line_counts"] = {
            str(kv): len(lines) for kv, lines in static.lines_by_kv.items() if kv >= min_kv
        }
        body["transmission_lines_by_kv"] = _LINES_PLACEHOLDER
    text = json.dumps(body, separators=(",", ":"))
    if "transmission_lines" in requested:
        text = text.replace(f'"{_LINES_PLACEHOLDER}"', _lines_json(prepared, min_kv), 1)
    return Response(content=text, media_type="application/json")
