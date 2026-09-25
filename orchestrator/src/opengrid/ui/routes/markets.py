"""Markets & feeds screen (02b S8 screen 4, UI-MKT subset). Owner: ui-b (BUILD.md S4).

Renders `/og/markets`: price/load/wind/solar/AS series charts, the forecast P10/P50/P90 band, and the
freshness/source-status table (LIVE vs SIM vs HIST, breaker state). Server-rendered first paint comes
from `opengrid.ui.api_client.get_json` (02b S7.1); the page polls itself every 30 s via `hx-trigger`
rather than SSE (02b S8: "market data does not need 2 s"). View-model functions below are pure and
unit-tested against JSON fixtures, no HTTP or DB involved.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime
from typing import Any

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse

from opengrid.ui.api_client import ApiUnavailable, get_json
from opengrid.ui.render import render_stale_badge, render_status_badge
from opengrid.ui.role import is_operator, role_of
from opengrid.ui.templating import templates

logger = logging.getLogger(__name__)
router = APIRouter(prefix="/markets")

SERIES_KEYS: tuple[str, ...] = ("price", "load", "wind", "solar", "as_price")

_SERIES_LABELS: dict[str, str] = {
    "price": "Wholesale price ($/MWh)",
    "load": "Load (MW)",
    "wind": "Wind (MW)",
    "solar": "Solar (MW)",
    "as_price": "AS price ($/MW)",
}

# `feed_status.source` values map to the LIVE/SIM/HIST badge (02b S8: "colour-blind-safe status
# colours ... not colour alone"). A source not in this map is shown as LIVE by default since the real
# feeds (ercot/eia/nws) are the common case; SIM and history-replay sources are the exceptions.
_HIST_OR_SIM_SOURCES: dict[str, str] = {"sim": "SIM", "history_replay": "HIST"}


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _age_seconds(ts: datetime, *, now: datetime) -> float:
    return max((now - ts).total_seconds(), 0.0)


def series_chart_view(observations: list[dict[str, Any]], *, series_key: str) -> dict[str, Any]:
    """One ECharts line-chart option per market series (price/load/wind/solar/AS), 02b S8 screen 4."""
    points = sorted(observations, key=lambda obs: obs["ts"])
    return {
        "series_key": series_key,
        "label": _SERIES_LABELS.get(series_key, series_key),
        "chart_option": {
            "xAxis": {"type": "category", "data": [point["ts"] for point in points]},
            "yAxis": {"type": "value"},
            "series": [
                {
                    "type": "line",
                    "name": _SERIES_LABELS.get(series_key, series_key),
                    "data": [point["value"] for point in points],
                    "showSymbol": False,
                }
            ],
            "tooltip": {"trigger": "axis"},
        },
        "latest_value": points[-1]["value"] if points else None,
        "latest_ts": points[-1]["ts"] if points else None,
        "unit": points[-1].get("unit") if points else None,
        "point_count": len(points),
    }


def forecast_band_view(forecast: dict[str, Any]) -> dict[str, Any]:
    """P10/P50/P90 forecast band as a stacked-area trick: an invisible P10 base, a shaded P10-to-P50
    band, the visible P50 line, and a shaded P50-to-P90 band (02b S8 screen 4)."""
    p10 = [float(v) for v in forecast.get("p10", [])]
    p50 = [float(v) for v in forecast.get("p50", [])]
    p90 = [float(v) for v in forecast.get("p90", [])]
    x_axis = forecast.get("ts") or list(range(len(p50)))
    band_low = p10
    band_mid = [mid - low for mid, low in zip(p50, p10, strict=False)]
    band_high = [high - mid for high, mid in zip(p90, p50, strict=False)]
    return {
        "chart_option": {
            "xAxis": {"type": "category", "data": x_axis},
            "yAxis": {"type": "value"},
            "legend": {},
            "tooltip": {"trigger": "axis"},
            "series": [
                {
                    "name": "P10",
                    "type": "line",
                    "stack": "band",
                    "symbol": "none",
                    "lineStyle": {"opacity": 0},
                    "areaStyle": {"opacity": 0},
                    "data": band_low,
                },
                {
                    "name": "P10-P50",
                    "type": "line",
                    "stack": "band",
                    "symbol": "none",
                    "lineStyle": {"opacity": 0},
                    "areaStyle": {"opacity": 0.25},
                    "data": band_mid,
                },
                {"name": "P50", "type": "line", "symbol": "none", "data": p50},
                {
                    "name": "P50-P90",
                    "type": "line",
                    "stack": "band",
                    "symbol": "none",
                    "lineStyle": {"opacity": 0},
                    "areaStyle": {"opacity": 0.25},
                    "data": band_high,
                },
            ],
        },
        "step_count": len(p50),
    }


def freshness_table_view(feed_statuses: list[dict[str, Any]], *, now: datetime) -> list[dict[str, Any]]:
    """Freshness/source-status table: age of the latest accepted value, breaker state, and the
    LIVE/SIM/HIST badge (02b S8 screen 4)."""
    rows: list[dict[str, Any]] = []
    for status in feed_statuses:
        last_value_at = status.get("last_value_at")
        age_s = _age_seconds(_parse_ts(last_value_at), now=now) if last_value_at else None
        rows.append(
            {
                "source": status.get("source", "unknown"),
                "product": status.get("product", "unknown"),
                "mode": _HIST_OR_SIM_SOURCES.get(str(status.get("source", "")).lower(), "LIVE"),
                "age_s": age_s,
                "since_iso": last_value_at,
                "consecutive_failures": status.get("consecutive_failures", 0),
                "breaker_open": bool(status.get("breaker_open", False)),
            }
        )
    return rows


def _to_freshness_table_row(row: dict[str, Any]) -> dict[str, Any]:
    """Render a `freshness_table_view` row into `data_table.html`-ready HTML cells (status/stale badges
    are colour + icon + text, never colour alone, per 02b S8's accessibility rule)."""
    breaker_status = "critical" if row["breaker_open"] else "good"
    return {
        "source": row["source"],
        "product": row["product"],
        "mode_badge": render_status_badge("info", label=row["mode"]),
        "age_badge": render_stale_badge(row["age_s"], since_iso=row.get("since_iso"), stale_after_s=1800),
        "consecutive_failures": row["consecutive_failures"],
        "breaker_badge": render_status_badge(
            breaker_status, label="OPEN" if row["breaker_open"] else "closed"
        ),
    }


@router.get("", response_class=HTMLResponse)
async def markets_page(request: Request) -> HTMLResponse:
    """Markets & feeds screen (`/og/markets`, viewer role read-only, 30 s poll not SSE per 02b S8)."""
    now = datetime.now(tz=UTC)
    degraded: str | None = None
    series_charts: list[dict[str, Any]] = []
    forecast: dict[str, Any] = {}
    feed_statuses: list[dict[str, Any]] = []

    for series_key in SERIES_KEYS:
        try:
            observations = await get_json("/og/api/markets/series", params={"series_key": series_key})
            series_charts.append(
                series_chart_view(
                    observations if isinstance(observations, list) else [], series_key=series_key
                )
            )
        except ApiUnavailable as exc:
            logger.warning("markets: series %s unavailable: %s", series_key, exc)
            degraded = degraded or str(exc)
            series_charts.append(series_chart_view([], series_key=series_key))

    try:
        # `kind` must be one of `opengrid.forecast.models.ForecastKind` ("price"/"load") -- the API
        # rejects anything else. This screen's forecast band is the wholesale price forecast, so "price"
        # is both a valid kind and the correct one; "quantiles" (fixed here, BUILD.md code-review round
        # item 6) was never a valid `kind` value and would have 422'd against a real `og-api`.
        forecast = await get_json("/og/api/forecast", params={"series_key": "price", "kind": "price"})
    except ApiUnavailable as exc:
        logger.warning("markets: forecast unavailable: %s", exc)
        degraded = degraded or str(exc)

    try:
        health = await get_json("/og/api/health")
        feed_statuses = health.get("feeds", []) if isinstance(health, dict) else []
    except ApiUnavailable as exc:
        logger.warning("markets: health/feeds unavailable: %s", exc)
        degraded = degraded or str(exc)

    return templates.TemplateResponse(
        request,
        "markets.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "series_charts": series_charts,
            "forecast": forecast_band_view(forecast if isinstance(forecast, dict) else {}),
            "freshness_rows": [
                _to_freshness_table_row(row) for row in freshness_table_view(feed_statuses, now=now)
            ],
            "degraded": degraded,
        },
    )
