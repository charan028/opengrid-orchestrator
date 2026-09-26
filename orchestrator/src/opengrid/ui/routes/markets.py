"""Markets & feeds screen (02b S8 screen 4, UI-MKT subset). Owner: ui-b (BUILD.md S4).

Renders `/og/markets`: price/load/wind/solar/AS series charts, the forecast P10/P50/P90 band, and the
freshness/source-status table (LIVE vs SIM vs HIST, breaker state). Server-rendered first paint comes
from `opengrid.ui.api_client.get_json` (02b S7.1); the page polls itself every 30 s via `hx-trigger`
rather than SSE (02b S8: "market data does not need 2 s"). View-model functions below are pure and
unit-tested against JSON fixtures, no HTTP or DB involved.
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
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

# What `GET /og/api/markets/series` needs for each screen series (found on the first live run: the API
# filters on the raw `feed_obs.series` value, which is a zone / sub-series name, never "price" or "load",
# so every chart came back empty). One product per screen series; `series` narrows to one sub-series
# where the product carries several kinds of rows, and is left open where the sub-series are the four
# load zones, which `series_chart_view` then plots as one line each. Product ids: `[feeds.ercot].products`.
_SERIES_QUERY: dict[str, dict[str, str]] = {
    "price": {"product": "np6-905-cd"},  # settlement point prices, one line per LZ_* zone
    "load": {"product": "np6-345-cd", "series_key": "total"},
    "wind": {"product": "np4-732-cd", "series_key": "actual"},
    "solar": {"product": "np4-737-cd", "series_key": "actual"},
    "as_price": {"product": "np4-188-cd", "series_key": "RRS"},
}

# `og.forecast.series_key` is a load zone (`opengrid.forecast.service` forecasts each LZ_* zone for kind
# "price"/"load"); `series_key="price"` matched nothing live. The band shows one zone.
# ponytail: fixed zone; add a zone selector to the screen if operators need the others.
_FORECAST_ZONE = "LZ_NORTH"

# `feed_status.source` values map to the LIVE/SIM/HIST badge (02b S8: "colour-blind-safe status
# colours ... not colour alone"). A source not in this map is shown as LIVE by default since the real
# feeds (ercot/eia/nws) are the common case; SIM and history-replay sources are the exceptions.
_HIST_OR_SIM_SOURCES: dict[str, str] = {"sim": "SIM", "history_replay": "HIST"}

# The bid funnel's four stages in the order they happen, with the operator-facing label for each. Fixed
# order, not the payload's: a funnel that reorders itself between polls is unreadable, and "rejected"
# last is what makes the drop from "we submitted" legible as a loss.
_BID_FUNNEL_STAGES: tuple[tuple[str, str], ...] = (
    ("available", "Made available by ERCOT"),
    ("submitted", "We submitted"),
    ("awarded", "We won"),
    ("rejected", "Rejected"),
)

# How far back the funnel panel looks. Matches the screen's other "recent activity" framing rather than
# the settlement day, because the question it answers ("are we leaving offers on the table right now?")
# is an operational one. ponytail: fixed window; add a range picker if anyone asks for last-week views.
_BID_FUNNEL_WINDOW = timedelta(hours=24)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _age_seconds(ts: datetime, *, now: datetime) -> float:
    return max((now - ts).total_seconds(), 0.0)


def series_chart_view(observations: list[dict[str, Any]], *, series_key: str) -> dict[str, Any]:
    """One ECharts line-chart option per market series (price/load/wind/solar/AS), 02b S8 screen 4."""
    points = sorted(observations, key=lambda obs: obs["ts"])
    # One ECharts line per distinct `series` value (the four load zones for the price product); a
    # single-series product still renders as one line named after the screen series.
    by_series: dict[str, list[dict[str, Any]]] = {}
    for point in points:
        by_series.setdefault(str(point.get("series") or series_key), []).append(point)
    x_axis = sorted({point["ts"] for point in points})
    lines = [
        {
            "type": "line",
            "name": name if len(by_series) > 1 else _SERIES_LABELS.get(series_key, series_key),
            "data": [{p["ts"]: p["value"] for p in pts}.get(ts) for ts in x_axis],
            "showSymbol": False,
        }
        for name, pts in by_series.items()
    ]
    latest_ts = x_axis[-1] if x_axis else None
    latest = [p["value"] for p in points if p["ts"] == latest_ts]
    return {
        "series_key": series_key,
        "label": _SERIES_LABELS.get(series_key, series_key),
        "chart_option": {
            "xAxis": {"type": "category", "data": x_axis},
            "yAxis": {"type": "value"},
            # Eight load zones do not fit a 220px tile as a centred legend; scroll it on one row and
            # leave room for it above the plot.
            "legend": {"show": len(lines) > 1, "type": "scroll", "top": 0},
            "grid": {
                "top": 28 if len(lines) > 1 else 10,
                "left": 8,
                "right": 8,
                "bottom": 8,
                "containLabel": True,
            },
            "series": lines,
            "tooltip": {"trigger": "axis"},
        },
        "latest_value": round(sum(latest) / len(latest), 2) if latest else None,
        "latest_ts": latest_ts,
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
                    "itemStyle": {"color": "token:--accent@0"},
                    "data": band_low,
                },
                {
                    "name": "P10-P50",
                    "type": "line",
                    "stack": "band",
                    "symbol": "none",
                    "lineStyle": {"opacity": 0},
                    "areaStyle": {"color": "token:--accent@0.18"},
                    "itemStyle": {"color": "token:--accent@0.4"},
                    "data": band_mid,
                },
                {
                    "name": "P50",
                    "type": "line",
                    "symbol": "none",
                    "lineStyle": {"color": "token:--accent", "width": 2},
                    "itemStyle": {"color": "token:--accent"},
                    "data": p50,
                },
                {
                    "name": "P50-P90",
                    "type": "line",
                    "stack": "band",
                    "symbol": "none",
                    "lineStyle": {"opacity": 0},
                    "areaStyle": {"color": "token:--accent@0.18"},
                    "itemStyle": {"color": "token:--accent@0.4"},
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


def _pct_of(part: float, whole: float) -> float:
    """A percentage of `whole`, 1 dp. A zero denominator is 0%, not an error: a window in which ERCOT
    offered nothing (or in which we bid on nothing) is a normal quiet hour, and the panel has to render
    it rather than 500 the whole markets screen on a division."""
    return round(100.0 * part / whole, 1) if whole else 0.0


def _reason_label(code: str) -> str:
    """`PRICE_ABOVE_CLEARING` -> `Price above clearing`. The raw code stays on the row next to this so an
    operator can quote it back to ERCOT verbatim; only the display text is softened."""
    words = str(code).replace("_", " ").strip()
    return words[:1].upper() + words[1:].lower() if words else str(code)


def bid_funnel_view(payload: dict[str, Any] | None, *, now: datetime) -> dict[str, Any]:
    """Bid funnel: ERCOT opportunities seen -> bids submitted -> awards won -> rejections, with the
    reasons (CR #19 item 4).

    Provisional contract. `GET /og/api/markets/bid-funnel?from=&to=` is still being built by the
    market-adapter team and 404s today, so this view is written against the agreed shape and the panel
    shows an explanatory empty state until the endpoint lands:

        {"from": "2026-09-26T00:00:00Z", "to": "2026-09-27T00:00:00Z",
         "products": [{"product": "NSPIN", "available": 24, "submitted": 18, "awarded": 11,
                       "rejected": 7,
                       "rejection_reasons": [{"reason": "PRICE_ABOVE_CLEARING", "count": 5},
                                             {"reason": "INSUFFICIENT_CAPACITY", "count": 2}]}],
         "by_hour": [{"hour": "2026-09-26T14:00:00Z", "available": 4, "submitted": 3, "awarded": 2,
                      "rejected": 1}]}

    Every stage percentage is of `available`, so the four bars read as one funnel narrowing from what the
    market offered -- a percentage of the previous stage would hide that we never bid on half of it.
    ponytail: `by_hour` is deliberately not rendered. The totals answer "how leaky is the funnel"; add an
    hourly chart when someone needs to know *when* it leaks.
    """
    # API shape (`routers.markets_funnel`): `by_product[]` and top-level `rejection_reasons[]`
    # (`reason_code`, `count`); the provisional shape (`products[]` with per-product reasons) still parses.
    raw_products: list[dict[str, Any]] = []
    if isinstance(payload, dict):
        raw_products = payload.get("by_product") or payload.get("products") or []
    rows: list[dict[str, Any]] = []
    for product in raw_products:
        counts = {key: int(product.get(key) or 0) for key, _ in _BID_FUNNEL_STAGES}
        rows.append(
            {
                "product": str(product.get("product") or product.get("service_type") or "unknown"),
                **counts,
                # Win rate is of what we *bid*, not of what ERCOT offered: the opportunities we skipped
                # were a bidding decision, not a loss, and folding them in would hide how well we price.
                "win_rate_pct": _pct_of(counts["awarded"], counts["submitted"]),
            }
        )

    reason_counts: dict[str, int] = {}
    top_reasons = payload.get("rejection_reasons") if isinstance(payload, dict) else None
    reason_entries = (
        top_reasons
        if isinstance(top_reasons, list)
        else [entry for product in raw_products for entry in product.get("rejection_reasons") or []]
    )
    for entry in reason_entries:
        code = str(entry.get("reason_code") or entry.get("reason") or "UNKNOWN")
        reason_counts[code] = reason_counts.get(code, 0) + int(entry.get("count") or 0)

    totals = {key: sum(row[key] for row in rows) for key, _ in _BID_FUNNEL_STAGES}
    return {
        # No products at all means the endpoint is not serving yet (or the window is empty); the template
        # branches on this to explain that rather than drawing four 0% bars that look like a total loss.
        "has_data": bool(rows),
        "stages": [
            {
                "key": key,
                "label": label,
                "count": totals[key],
                "pct": _pct_of(totals[key], totals["available"]),
            }
            for key, label in _BID_FUNNEL_STAGES
        ]
        if rows
        else [],
        "rows": rows,
        "reasons": [
            {
                "reason": code,
                "label": _reason_label(code),
                "count": count,
                "pct_of_rejected": _pct_of(count, totals["rejected"]),
            }
            # Biggest reason first, code as the tie-break: two reasons biting equally often must not
            # swap places between 30 s polls, or the list flickers under the operator's eye.
            for code, count in sorted(reason_counts.items(), key=lambda kv: (-kv[1], kv[0]))
        ],
        "totals": totals,
        "generated_at": now.isoformat(),
    }


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
            observations = await get_json("/og/api/markets/series", params=_SERIES_QUERY[series_key])
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
        forecast = await get_json("/og/api/forecast", params={"series_key": _FORECAST_ZONE, "kind": "price"})
    except ApiUnavailable as exc:
        logger.warning("markets: forecast unavailable: %s", exc)
        degraded = degraded or str(exc)

    try:
        health = await get_json("/og/api/health")
        feed_statuses = health.get("feeds", []) if isinstance(health, dict) else []
    except ApiUnavailable as exc:
        logger.warning("markets: health/feeds unavailable: %s", exc)
        degraded = degraded or str(exc)

    # Bid funnel (CR #19 item 4). The endpoint is still being built and 404s today, so a failure here is
    # expected, not an incident: log at INFO and leave `degraded` alone, otherwise every markets page
    # would wear a degraded banner for a panel everyone knows is pending. The panel explains itself.
    bid_funnel = bid_funnel_view(None, now=now)
    try:
        funnel = await get_json(
            "/og/api/markets/bid-funnel",
            params={"from": (now - _BID_FUNNEL_WINDOW).isoformat(), "to": now.isoformat()},
        )
        bid_funnel = bid_funnel_view(funnel if isinstance(funnel, dict) else None, now=now)
    except ApiUnavailable as exc:
        logger.info("markets: bid-funnel endpoint not available yet: %s", exc)

    return templates.TemplateResponse(
        request,
        "markets.html",
        {
            "role": role_of(request),
            "is_operator": is_operator(request),
            "series_charts": series_charts,
            "forecast": forecast_band_view(forecast if isinstance(forecast, dict) else {}),
            "bid_funnel": bid_funnel,
            "freshness_rows": [
                _to_freshness_table_row(row) for row in freshness_table_view(feed_statuses, now=now)
            ],
            "degraded": degraded,
        },
    )
