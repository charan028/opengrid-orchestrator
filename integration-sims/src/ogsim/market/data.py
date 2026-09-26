"""Builds ERCOT/EIA/NWS-shaped response payloads from either replay or
synthetic data, with market anomalies applied.

Field names, query params, the response envelope and timestamp handling are
fixed by `interfaces/http/market-api.md` §1 (`opengrid.feeds` is built
against that document, not against this code). ERCOT pagination shape:

    {"_meta": {"totalRecords": N, "totalPages": P, "currentPage": p, "pageSize": s},
     "fields": [{"name": "...", "dataType": "DATE"|"INTEGER"|"STRING"|"NUMBER"|"BOOLEAN"}, ...],
     "data": [[...], [...], ...]}

Per market-api.md §1's response envelope, `data` cells carry their real JSON
type (numbers/booleans, not stringified) and `fields` order must match each
product's `data` row column order exactly.

All ERCOT delivery timestamps are America/Chicago local wall-clock (never
UTC, never carrying an offset suffix): `deliveryDate`/`operatingDay` +
`deliveryHour`/`hourEnding` (1-24, hour-ending) + an optional 15-minute
`deliveryInterval` (1-4). `DSTFlag` is `true` only for the one repeated
wall-clock hour on the fall-back DST transition day; Python's `datetime.fold`
(set automatically by `astimezone` per PEP 495) identifies that hour.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any
from zoneinfo import ZoneInfo

from ogsim.market import synthetic
from ogsim.market.anomalies import AnomalyStore
from ogsim.market.config import MarketConfig
from ogsim.market.replay import HistoryReplay

PRODUCTS = {
    "np6-905-cd": "spp_node_zone_hub",
    "np6-345-cd": "act_sys_load_by_wzn",
    "np4-732-cd": "wpp_hrly_avrg_actl_fcast",
    "np4-737-cd": "spp_hrly_avrg_actl_fcast",
    "np4-188-cd": "dam_clear_price_for_cap",
}

DEFAULT_PAGE_SIZE = 1000
DEFAULT_SETTLEMENT_POINT_TYPE = "LZ"

# interfaces/http/market-api.md §1: America/Chicago local wall-clock for
# every ERCOT delivery timestamp.
ERCOT_LOCAL_TZ = ZoneInfo("America/Chicago")
MINUTES_PER_SPP_INTERVAL = 15
DEFAULT_SPP_HOURS_BACK = 3.0
DEFAULT_LOAD_DAYS_BACK = 1.0
DEFAULT_RENEWABLE_HOURS_BACK = 6.0
DEFAULT_AS_DAYS_BACK = 1.0
EIA_WINDOW_HOURS = 24
NWS_FORECAST_PERIODS = 48
NWS_UPDATED_TRUNCATE_MINUTE = 0

DEFAULT_PRICE_SPIKE_USD_PER_MWH = 5000.0
DEFAULT_NEGATIVE_PRICE_USD_PER_MWH = -50.0
DEFAULT_AS_JUMP_USD_PER_MWH = 500.0

KW_TO_MW = 1000.0
# substation_load_kw is a single feeder's reading; scale it up to a
# system-wide zone total so replayed load lands in the same order of
# magnitude as the synthetic curves (~thousands of MW per zone).
REPLAY_ZONE_LOAD_SCALE = 50.0

ZONE_LOAD_SHARE = {
    "coast": 0.20,
    "east": 0.06,
    "farWest": 0.05,
    "north": 0.08,
    "northC": 0.23,
    "southern": 0.09,
    "southC": 0.14,
    "west": 0.07,
}
DEFAULT_ZONE_LOAD_SHARE = 0.10

CELSIUS_TO_FAHRENHEIT_SCALE = 9 / 5
CELSIUS_TO_FAHRENHEIT_OFFSET = 32

PRICE_DECIMALS = 2
LOAD_DECIMALS = 1

# (name, dataType) per interfaces/http/market-api.md §1's response envelope
# and its per-product "Columns consumed" table.
SPP_FIELDS = [
    ("deliveryDate", "DATE"),
    ("deliveryHour", "INTEGER"),
    ("deliveryInterval", "INTEGER"),
    ("settlementPoint", "STRING"),
    ("settlementPointType", "STRING"),
    ("settlementPointPrice", "NUMBER"),
    ("DSTFlag", "BOOLEAN"),
]
# NP6-345-CD is "wide": one column per weather zone plus a system `total`,
# in the exact order market-api.md documents them.
LOAD_FIELDS = [
    ("operatingDay", "DATE"),
    ("hourEnding", "STRING"),
    ("DSTFlag", "BOOLEAN"),
    *((zone, "NUMBER") for zone in synthetic.WEATHER_ZONES),
    ("total", "NUMBER"),
]
WIND_FIELDS = [
    ("deliveryDate", "DATE"),
    ("hourEnding", "INTEGER"),
    ("DSTFlag", "BOOLEAN"),
    ("genSystemWide", "NUMBER"),
    ("STWPFSystemWide", "NUMBER"),
]
SOLAR_FIELDS = [
    ("deliveryDate", "DATE"),
    ("hourEnding", "INTEGER"),
    ("DSTFlag", "BOOLEAN"),
    ("genSystemWide", "NUMBER"),
    ("STPPFSystemWide", "NUMBER"),
]
AS_FIELDS = [
    ("deliveryDate", "DATE"),
    ("hourEnding", "STRING"),
    ("DSTFlag", "BOOLEAN"),
    ("ancillaryType", "STRING"),
    ("MCPC", "NUMBER"),
]


def _local(ts: datetime) -> datetime:
    """Converts an aware UTC-ish timestamp to America/Chicago wall-clock,
    per market-api.md §1. `astimezone` sets `.fold` correctly (PEP 495) so
    the one repeated wall-clock hour on a fall-back DST day is identifiable."""
    return ts.astimezone(ERCOT_LOCAL_TZ)


def _hour_ending(local: datetime) -> int:
    """Hour-ending convention (1-24): the wall-clock hour `[local.hour,
    local.hour + 1)` is reported as `local.hour + 1`."""
    return local.hour + 1


def _hour_ending_label(hour_ending: int) -> str:
    """`hourEnding` formatted `"HH:MM"` (e.g. `"01:00"`..`"24:00"`), used by
    the products that render it as a string per market-api.md §1's table."""
    return f"{hour_ending:02d}:00"


def _delivery_interval(local: datetime) -> int:
    """15-minute interval-ending within the hour (1-4)."""
    return local.minute // MINUTES_PER_SPP_INTERVAL + 1


def _dst_flag(local: datetime) -> bool:
    return bool(local.fold)


def paginate(rows: list[list[Any]], page: int, size: int) -> tuple[list[list[Any]], dict[str, Any]]:
    total = len(rows)
    size = max(1, size)
    page = max(1, page)
    total_pages = max(1, (total + size - 1) // size)
    start = (page - 1) * size
    page_rows = rows[start : start + size]
    meta = {
        "totalRecords": total,
        "totalPages": total_pages,
        "currentPage": page,
        "pageSize": size,
    }
    return page_rows, meta


def report_response(
    fields: list[tuple[str, str]], rows: list[list[Any]], page: int, size: int
) -> dict[str, Any]:
    page_rows, meta = paginate(rows, page, size)
    return {
        "_meta": meta,
        "fields": [{"name": name, "dataType": data_type} for name, data_type in fields],
        "data": page_rows,
    }


def _effective_now(now: datetime, anomalies: AnomalyStore, product: str) -> datetime:
    """stale_posting: freeze the virtual clock at the anomaly's start time
    for as long as it's active, so no new data appears to post."""
    for a in anomalies.active(product, now.timestamp()):
        if a.type == "stale_posting":
            return datetime.fromtimestamp(a.start, tz=UTC)
    return now


def _price_override(anomalies: AnomalyStore, product: str, now: datetime) -> float | None:
    for a in anomalies.active(product, now.timestamp()):
        if a.type == "price_spike":
            return float(a.params.get("value_usd_per_mwh", DEFAULT_PRICE_SPIKE_USD_PER_MWH))
        if a.type == "negative_price":
            return float(a.params.get("value_usd_per_mwh", DEFAULT_NEGATIVE_PRICE_USD_PER_MWH))
    return None


def _as_price_override(anomalies: AnomalyStore, product: str, now: datetime, service: str) -> float | None:
    for a in anomalies.active(product, now.timestamp()):
        if a.type == "as_price_jump" and a.params.get("service", service) == service:
            return float(a.params.get("value_usd_per_mwh", DEFAULT_AS_JUMP_USD_PER_MWH))
    return None


def _nws_override(anomalies: AnomalyStore, now: datetime) -> dict[str, Any] | None:
    for a in anomalies.active("nws", now.timestamp()):
        if a.type == "nws_extreme_weather":
            return a.params
    return None


class MarketData:
    def __init__(self, cfg: MarketConfig, anomalies: AnomalyStore):
        self.cfg = cfg
        self.anomalies = anomalies
        self.replay = HistoryReplay(cfg.history_path) if cfg.data_mode == "replay" else None
        self.use_replay = bool(self.replay and self.replay.available)

    def _replay_value_at(self, signal_type: str, ts: datetime) -> float | None:
        if self.replay is None:
            return None
        return self.replay.value_at(signal_type, ts)

    # ---- ERCOT: NP6-905-CD settlement point prices --------------------
    def spp_rows(
        self,
        now: datetime,
        settlement_point_type: str = DEFAULT_SETTLEMENT_POINT_TYPE,
        settlement_point: str | None = None,
        hours_back: float = DEFAULT_SPP_HOURS_BACK,
    ) -> list[list[Any]]:
        product = "np6-905-cd"
        eff_now = _effective_now(now, self.anomalies, product)
        override = _price_override(self.anomalies, product, now)
        points = synthetic.HUBS if settlement_point_type == "HU" else synthetic.LOAD_ZONES
        if settlement_point:
            points = [settlement_point]
        rows: list[list[Any]] = []
        intervals_per_hour = 60 // MINUTES_PER_SPP_INTERVAL
        n_steps = int(hours_back * intervals_per_hour)
        for i in range(n_steps, -1, -1):
            ts = eff_now - timedelta(minutes=MINUTES_PER_SPP_INTERVAL * i)
            local = _local(ts)
            delivery_date = local.date().isoformat()
            hour_ending = _hour_ending(local)
            interval = _delivery_interval(local)
            dst_flag = _dst_flag(local)
            for point in points:
                if override is not None and i == 0:
                    price = override
                elif self.use_replay:
                    base = self._replay_value_at("wholesale_price_mwh", ts)
                    price = (
                        base if base is not None else synthetic.price_usd_per_mwh(ts, self.cfg.seed, point)
                    )
                else:
                    price = synthetic.price_usd_per_mwh(ts, self.cfg.seed, point)
                rows.append(
                    [
                        delivery_date,
                        hour_ending,
                        interval,
                        point,
                        settlement_point_type,
                        round(float(price), PRICE_DECIMALS),
                        dst_flag,
                    ]
                )
        return rows

    # ---- ERCOT: NP6-345-CD actual system load by weather zone ----------
    def load_rows(self, now: datetime, days_back: float = DEFAULT_LOAD_DAYS_BACK) -> list[list[Any]]:
        product = "np6-345-cd"
        eff_now = _effective_now(now, self.anomalies, product)
        rows: list[list[Any]] = []
        n_steps = int(days_back * 24)
        for i in range(n_steps, -1, -1):
            ts = eff_now - timedelta(hours=i)
            local = _local(ts)
            operating_day = local.date().isoformat()
            hour_ending_label = _hour_ending_label(_hour_ending(local))
            dst_flag = _dst_flag(local)
            zone_loads: list[float] = []
            for zone in synthetic.WEATHER_ZONES:
                if self.use_replay:
                    base = self._replay_value_at("substation_load_kw", ts)
                    if base is not None:
                        weight = ZONE_LOAD_SHARE.get(zone, DEFAULT_ZONE_LOAD_SHARE)
                        load_mw = round((base / KW_TO_MW) * weight * REPLAY_ZONE_LOAD_SCALE, LOAD_DECIMALS)
                    else:
                        load_mw = synthetic.load_mw(ts, self.cfg.seed, zone)
                else:
                    load_mw = synthetic.load_mw(ts, self.cfg.seed, zone)
                zone_loads.append(load_mw)
            total_mw = round(sum(zone_loads), LOAD_DECIMALS)
            rows.append([operating_day, hour_ending_label, dst_flag, *zone_loads, total_mw])
        return rows

    # ---- ERCOT: NP4-732/737-CD wind/solar actual vs forecast ------------
    def renewable_rows(
        self, now: datetime, kind: str, hours_back: float = DEFAULT_RENEWABLE_HOURS_BACK
    ) -> list[list[Any]]:
        product = "np4-732-cd" if kind == "wind" else "np4-737-cd"
        eff_now = _effective_now(now, self.anomalies, product)
        rows: list[list[Any]] = []
        n_steps = int(hours_back)
        gen = synthetic.wind_mw if kind == "wind" else synthetic.solar_mw
        for i in range(n_steps, -1, -1):
            ts = eff_now - timedelta(hours=i)
            local = _local(ts)
            delivery_date = local.date().isoformat()
            hour_ending = _hour_ending(local)
            dst_flag = _dst_flag(local)
            actual, forecast = gen(ts, self.cfg.seed)
            rows.append([delivery_date, hour_ending, dst_flag, actual, forecast])
        return rows

    # ---- ERCOT: NP4-188-CD DAM clearing prices for capacity -------------
    def as_rows(self, now: datetime, days_back: float = DEFAULT_AS_DAYS_BACK) -> list[list[Any]]:
        product = "np4-188-cd"
        eff_now = _effective_now(now, self.anomalies, product)
        rows: list[list[Any]] = []
        n_days = max(1, int(days_back))
        for d in range(n_days, -1, -1):
            ts = eff_now - timedelta(days=d)
            local = _local(ts)
            delivery_date = local.date().isoformat()
            hour_ending_label = _hour_ending_label(_hour_ending(local))
            dst_flag = _dst_flag(local)
            for service in synthetic.AS_SERVICES:
                override = _as_price_override(self.anomalies, product, now, service)
                price = (
                    override
                    if override is not None
                    else synthetic.as_clearing_price(ts, self.cfg.seed, service)
                )
                rows.append(
                    [
                        delivery_date,
                        hour_ending_label,
                        dst_flag,
                        service,
                        round(float(price), PRICE_DECIMALS),
                    ]
                )
        return rows

    # ---- EIA v2 electricity/rto/region-data ------------------------------
    def eia_response(self, now: datetime, respondent: str = "ERCO") -> dict[str, Any]:
        eff_now = _effective_now(now, self.anomalies, "eia")
        rows = []
        for i in range(EIA_WINDOW_HOURS, -1, -1):
            ts = eff_now - timedelta(hours=i)
            zone_totals = [synthetic.load_mw(ts, self.cfg.seed, z) for z in synthetic.WEATHER_ZONES]
            value = round(sum(zone_totals), 1)
            rows.append(
                {
                    "period": ts.strftime("%Y-%m-%dT%H"),
                    "respondent": respondent,
                    "type": "D",
                    "type-name": "Demand",
                    "value": f"{value}",
                    "value-units": "megawatthours",
                }
            )
        return {
            "response": {"total": str(len(rows)), "data": rows},
            "request": {"command": "/v2/electricity/rto/region-data/data/", "params": {}},
        }

    # ---- NWS -------------------------------------------------------------
    def nws_points(self, lat: float, lon: float) -> dict[str, Any]:
        return {
            "properties": {
                "gridId": "EWX",
                "gridX": 156,
                "gridY": 91,
                "forecastHourly": "https://api.weather.gov/gridpoints/EWX/156,91/forecast/hourly",
                "relativeLocation": {
                    "properties": {"city": "Simulated", "state": "TX"},
                    "geometry": {"type": "Point", "coordinates": [lon, lat]},
                },
            }
        }

    def nws_updated_at(self, now: datetime) -> datetime:
        """The hourly-forecast "updated" timestamp: truncated to the hour, so
        `If-Modified-Since` can be compared against a stable value between
        polls within the same hour (matches the real hourly cadence)."""
        return now.replace(minute=NWS_UPDATED_TRUNCATE_MINUTE, second=0, microsecond=0)

    def nws_hourly_forecast(self, now: datetime, office: str, x: int, y: int) -> dict[str, Any]:
        override = _nws_override(self.anomalies, now)
        updated = self.nws_updated_at(now)
        periods = []
        for i in range(NWS_FORECAST_PERIODS):
            ts = now + timedelta(hours=i)
            base = synthetic.nws_forecast_period(now, self.cfg.seed, i)
            if override:
                base = {
                    **base,
                    "temperature_c": float(override.get("temperature_c", base["temperature_c"])),
                    "wind_kph": float(override.get("wind_kph", base["wind_kph"])),
                    "short_forecast": override.get("condition", base["short_forecast"]),
                }
            periods.append(
                {
                    "number": i + 1,
                    "startTime": ts.isoformat(),
                    "endTime": (ts + timedelta(hours=1)).isoformat(),
                    "temperature": round(
                        base["temperature_c"] * CELSIUS_TO_FAHRENHEIT_SCALE + CELSIUS_TO_FAHRENHEIT_OFFSET
                    ),
                    "temperatureUnit": "F",
                    "dewpoint": {"unitCode": "wmoUnit:degC", "value": base["dewpoint_c"]},
                    "skyCover": base["sky_cover_pct"],
                    "shortForecast": base["short_forecast"],
                }
            )
        return {
            "properties": {
                "gridId": office,
                "gridX": x,
                "gridY": y,
                "updated": updated.isoformat(),
                "periods": periods,
            }
        }
