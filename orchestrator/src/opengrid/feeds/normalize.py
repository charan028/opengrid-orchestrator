"""Normalization of raw ERCOT/EIA/NWS payloads into `opengrid.core.models.platform.FeedObs` rows
(02b S2.7).

Pure functions: no I/O, no network, no clock reads except a passed-in `recorded_at`, so they are unit
tested with fixture payloads (`interfaces/http/market-api.md` shapes) alone. Each function raises
`FeedDataError` on a malformed payload -- `feeds.ercot`/`eia`/`nws` decide whether that counts as a
breaker failure.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from zoneinfo import ZoneInfo

from opengrid.core.models.platform import FeedObs

SOURCE_ERCOT = "ERCOT"
SOURCE_EIA = "EIA"
SOURCE_NWS = "NWS"

# ERCOT's `deliveryDate`/`operatingDay` + `deliveryHour`/`hourEnding`/`deliveryInterval` fields are all
# America/Chicago LOCAL clock values (never UTC, and never carrying their own offset) -- confirmed
# against a live NP6-905-CD/NP6-345-CD/NP4-188-CD call (BUILD.md follow-up finding: the field-name
# mismatch investigation also surfaced that timestamps must be localized, not treated as bare UTC).
_CHICAGO = ZoneInfo("America/Chicago")
_MINUTES_PER_QUARTER_HOUR = 15


def _chicago_local_to_utc(naive_local: datetime, *, dst_flag: bool) -> datetime:
    """Localize an America/Chicago wall-clock `datetime` (naive) to UTC.

    `dst_flag` is ERCOT's `DSTFlag` field: per ERCOT's own documentation it is `True` only for the one
    repeated wall-clock hour on the fall-back transition day (not a general "currently observing DST"
    indicator), so it maps directly onto Python's `fold` -- `fold=1` selects the SECOND (later,
    standard-time) occurrence of an ambiguous local time. Every other date/time resolves to the same
    UTC instant regardless of `fold`, since `zoneinfo` already applies America/Chicago's normal
    CST/CDT rules from the calendar date.
    """
    return naive_local.replace(tzinfo=_CHICAGO, fold=1 if dst_flag else 0).astimezone(UTC)


# EIA is a same-shape system-wide substitute for load, published under one series key regardless of
# which ERCOT weather zone the caller ultimately wants (02b S2.3): the ERCO respondent has no zonal
# breakdown, so mapping it 1:1 onto `og.feed_obs` means picking a single system series key.
EIA_SYSTEM_LOAD_SERIES = "ERCOT_SYSTEM"


class FeedDataError(Exception):
    """A payload could not be normalized (missing `fields`, wrong row arity, unparseable value)."""


def _field_index(fields: list[dict[str, str]], name: str) -> int:
    for i, f in enumerate(fields):
        if f.get("name") == name:
            return i
    raise FeedDataError(f"field {name!r} not present in response `fields`")


def _parse_utc(value: str) -> datetime:
    dt = datetime.fromisoformat(value)
    return dt if dt.tzinfo else dt.replace(tzinfo=UTC)


def normalize_ercot_envelope(
    payload: dict[str, object], *, product: str, columns: dict[str, str], recorded_at: datetime
) -> list[dict[str, object]]:
    """Map the shared ERCOT response envelope (market-api.md S1) into row dicts keyed the same as
    `columns` (e.g. `{"ts": "deliveryDateTime", "series": "settlementPoint", "value":
    "settlementPointPrice"}`). Returns one dict per data row with keys `ts`, `series`, `value` (raw
    strings/values, not yet a `FeedObs`) -- product-specific callers turn these into `FeedObs`.
    """
    fields = payload.get("fields")
    rows = payload.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise FeedDataError(f"{product}: malformed envelope, missing fields/data")

    indices = {out_key: _field_index(fields, col_name) for out_key, col_name in columns.items()}
    max_index = max(indices.values())

    normalized: list[dict[str, object]] = []
    for row in rows:
        if not isinstance(row, list) or len(row) <= max_index:
            raise FeedDataError(f"{product}: data row has wrong arity: {row!r}")
        normalized.append({out_key: row[idx] for out_key, idx in indices.items()})
    return normalized


def _ercot_int_field(value: object) -> int:
    """ERCOT's hour-ending field is an `int` (1-24) on some products (wind/solar's `hourEnding`, SPP's
    `deliveryHour`) and a `"HH:MM"` string on others (load-by-weather-zone/AS-price's `hourEnding`) --
    confirmed against live responses for each product. `deliveryInterval` (1-4) is always a bare int.
    Accept either shape uniformly, taking only the leading `"HH"` of a colon-separated string."""
    if isinstance(value, bool):
        raise FeedDataError(f"expected an int/str field, got a bool: {value!r}")
    if isinstance(value, int):
        return value
    return int(str(value).split(":")[0])


def _ercot_interval_start_utc(
    date_str: str, hour_ending: int, *, interval_of_hour: int = 1, dst_flag: bool = False
) -> datetime:
    """Compose the UTC instant at the START of an ERCOT hour-ending (+ optional 15-minute
    sub-interval) delivery interval. `date_str`/`hour_ending`/`interval_of_hour` are all
    America/Chicago local wall-clock values (never UTC) -- confirmed against a live call; the
    original assumption of a single ready-made `deliveryDateTime`/`operatingDateTime` UTC column was
    wrong for every MVP-S product. `hour_ending` counts 1-24 (hour_ending=1 is the interval ending at
    01:00 local, i.e. starting at 00:00); `interval_of_hour` counts 1-4 fifteen-minute sub-intervals
    within that hour, ending at the hour boundary.
    """
    date_part = date_str.split("T")[0]
    naive_midnight = datetime.fromisoformat(f"{date_part}T00:00:00")
    naive_local = naive_midnight + timedelta(
        hours=hour_ending - 1, minutes=(interval_of_hour - 1) * _MINUTES_PER_QUARTER_HOUR
    )
    return _chicago_local_to_utc(naive_local, dst_flag=dst_flag)


def ercot_spp_to_feed_obs(
    payload: dict[str, object], *, product: str, recorded_at: datetime
) -> list[FeedObs]:
    """NP6-905-CD real-time settlement point price ($/MWh) per settlement point, 15-minute intervals.

    Real fields (confirmed live): `deliveryDate`, `deliveryHour`, `deliveryInterval`,
    `settlementPoint`, `settlementPointType`, `settlementPointPrice`, `DSTFlag` -- there is no single
    `deliveryDateTime` column (the originally-assumed shape); the interval-start timestamp is
    composed from date + hour-ending + 15-minute-interval-of-hour, localized America/Chicago -> UTC.
    """
    rows = normalize_ercot_envelope(
        payload,
        product=product,
        columns={
            "date": "deliveryDate",
            "hour": "deliveryHour",
            "interval": "deliveryInterval",
            "dst": "DSTFlag",
            "series": "settlementPoint",
            "value": "settlementPointPrice",
        },
        recorded_at=recorded_at,
    )
    return [
        FeedObs(
            source=SOURCE_ERCOT,
            product=product,
            series=str(r["series"]),
            ts=_ercot_interval_start_utc(
                str(r["date"]),
                _ercot_int_field(r["hour"]),
                interval_of_hour=_ercot_int_field(r["interval"]),
                dst_flag=bool(r["dst"]),
            ),
            value=float(str(r["value"])),
            unit="usd_per_mwh",
            quality="GOOD",
            recorded_at=recorded_at,
        )
        for r in rows
    ]


#: NP6-345-CD's real response is wide (one row per hour, one column per weather zone) rather than the
#: originally-assumed tall shape with a single `weatherZone`/`load` pair -- confirmed live.
_LOAD_ZONE_COLUMNS: tuple[str, ...] = (
    "coast",
    "east",
    "farWest",
    "north",
    "northC",
    "southern",
    "southC",
    "west",
    "total",
)


def ercot_load_to_feed_obs(
    payload: dict[str, object], *, product: str, recorded_at: datetime
) -> list[FeedObs]:
    """NP6-345-CD actual system load (MW) per weather zone, plus a `total` series. One row per hour,
    wide-format (`_LOAD_ZONE_COLUMNS`); each zone column becomes its own `FeedObs.series`. A zone's
    value can be `null` (not yet posted); such a zone is skipped for that hour rather than raising.
    """
    fields = payload.get("fields")
    rows = payload.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise FeedDataError(f"{product}: malformed envelope, missing fields/data")

    date_idx = _field_index(fields, "operatingDay")
    hour_idx = _field_index(fields, "hourEnding")
    dst_idx = _field_index(fields, "DSTFlag")
    zone_indices = {zone: _field_index(fields, zone) for zone in _LOAD_ZONE_COLUMNS}
    max_index = max(date_idx, hour_idx, dst_idx, *zone_indices.values())

    obs: list[FeedObs] = []
    for row in rows:
        if not isinstance(row, list) or len(row) <= max_index:
            raise FeedDataError(f"{product}: data row has wrong arity: {row!r}")
        ts = _ercot_interval_start_utc(
            str(row[date_idx]), _ercot_int_field(row[hour_idx]), dst_flag=bool(row[dst_idx])
        )
        for zone, idx in zone_indices.items():
            value = row[idx]
            if value is None:
                continue
            obs.append(
                FeedObs(
                    source=SOURCE_ERCOT,
                    product=product,
                    series=zone,
                    ts=ts,
                    value=float(str(value)),
                    unit="mw",
                    quality="GOOD",
                    recorded_at=recorded_at,
                )
            )
    return obs


def ercot_wind_to_feed_obs(
    payload: dict[str, object], *, product: str, recorded_at: datetime
) -> list[FeedObs]:
    """NP4-732-CD system-wide wind actual + forecast (MW), stored as two series: `actual`, `forecast`.

    Real fields (confirmed live): actual generation is `genSystemWide` (the originally-assumed
    `actualSystemWideWindOutput` does not exist), forecast is `STWPFSystemWide` (Short-Term Wind Power
    Forecast; the originally-assumed `windOutputForecastSystemWide` does not exist either).
    """
    return _ercot_actual_forecast_to_feed_obs(
        payload,
        product=product,
        actual_col="genSystemWide",
        forecast_col="STWPFSystemWide",
        recorded_at=recorded_at,
    )


def ercot_solar_to_feed_obs(
    payload: dict[str, object], *, product: str, recorded_at: datetime
) -> list[FeedObs]:
    """NP4-737-CD system-wide solar actual + forecast (MW), stored as two series: `actual`, `forecast`.

    Real fields (confirmed live): actual generation is `genSystemWide`, forecast is `STPPFSystemWide`
    (Short-Term Photovoltaic Power Forecast) -- the originally-assumed `actualSystemWideSolarOutput`/
    `solarOutputForecastSystemWide` columns do not exist.
    """
    return _ercot_actual_forecast_to_feed_obs(
        payload,
        product=product,
        actual_col="genSystemWide",
        forecast_col="STPPFSystemWide",
        recorded_at=recorded_at,
    )


def _ercot_actual_forecast_to_feed_obs(
    payload: dict[str, object],
    *,
    product: str,
    actual_col: str,
    forecast_col: str,
    recorded_at: datetime,
) -> list[FeedObs]:
    """Shared wind/solar normalizer. The interval timestamp comes from `deliveryDate`/`hourEnding`/
    `DSTFlag` (America/Chicago local, like every other MVP-S ERCOT product), not `postedDatetime`
    (which is the forecast's publish time, not the delivery interval it describes). `actual_col` is
    `null` for a not-yet-elapsed delivery hour (a forecast-horizon row) -- that series is skipped for
    such rows rather than raising, since the row is still valid data for the `forecast` series.
    """
    rows = normalize_ercot_envelope(
        payload,
        product=product,
        columns={
            "date": "deliveryDate",
            "hour": "hourEnding",
            "dst": "DSTFlag",
            "actual": actual_col,
            "forecast": forecast_col,
        },
        recorded_at=recorded_at,
    )
    obs: list[FeedObs] = []
    for r in rows:
        ts = _ercot_interval_start_utc(str(r["date"]), _ercot_int_field(r["hour"]), dst_flag=bool(r["dst"]))
        if r["actual"] is not None:
            obs.append(
                FeedObs(
                    source=SOURCE_ERCOT,
                    product=product,
                    series="actual",
                    ts=ts,
                    value=float(str(r["actual"])),
                    unit="mw",
                    quality="GOOD",
                    recorded_at=recorded_at,
                )
            )
        if r["forecast"] is not None:
            obs.append(
                FeedObs(
                    source=SOURCE_ERCOT,
                    product=product,
                    series="forecast",
                    ts=ts,
                    value=float(str(r["forecast"])),
                    unit="mw",
                    quality="GOOD",
                    recorded_at=recorded_at,
                )
            )
    return obs


def ercot_as_price_to_feed_obs(
    payload: dict[str, object], *, product: str, recorded_at: datetime
) -> list[FeedObs]:
    """NP4-188-CD day-ahead AS clearing price ($/MW-h), one series per ancillary product.

    Real field name (confirmed live): `MCPC` (uppercase) -- the originally-assumed lowercase `mcpc`
    does not exist. The interval timestamp is also now correctly localized America/Chicago -> UTC via
    `DSTFlag` (the previous `_parse_delivery_date_hour_ending` treated the local wall-clock hour as if
    it were already UTC, which was off by 5-6 hours depending on season).
    """
    fields = payload.get("fields")
    rows = payload.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise FeedDataError(f"{product}: malformed envelope, missing fields/data")

    date_idx = _field_index(fields, "deliveryDate")
    hour_idx = _field_index(fields, "hourEnding")
    type_idx = _field_index(fields, "ancillaryType")
    price_idx = _field_index(fields, "MCPC")
    dst_idx = _field_index(fields, "DSTFlag")
    max_index = max(date_idx, hour_idx, type_idx, price_idx, dst_idx)

    obs: list[FeedObs] = []
    for row in rows:
        if not isinstance(row, list) or len(row) <= max_index:
            raise FeedDataError(f"{product}: data row has wrong arity: {row!r}")
        ts = _ercot_interval_start_utc(
            str(row[date_idx]), _ercot_int_field(row[hour_idx]), dst_flag=bool(row[dst_idx])
        )
        obs.append(
            FeedObs(
                source=SOURCE_ERCOT,
                product=product,
                series=str(row[type_idx]),
                ts=ts,
                value=float(row[price_idx]),
                unit="usd_per_mwh",
                quality="GOOD",
                recorded_at=recorded_at,
            )
        )
    return obs


def eia_demand_to_feed_obs(payload: dict[str, object], *, recorded_at: datetime) -> list[FeedObs]:
    """EIA v2 hourly demand response (02b S2.3): standby fallback for system load, quality=ESTIMATED
    so downstream can distinguish it from ERCOT-primary readings."""
    response = payload.get("response")
    if not isinstance(response, dict) or not isinstance(response.get("data"), list):
        raise FeedDataError("EIA response missing response.data")

    obs: list[FeedObs] = []
    for row in response["data"]:
        try:
            period = str(row["period"])
            value = float(row["value"])
        except (KeyError, TypeError, ValueError) as exc:
            raise FeedDataError(f"EIA row malformed: {row!r}") from exc
        ts = _parse_eia_period(period)
        obs.append(
            FeedObs(
                source=SOURCE_EIA,
                product="eia-demand",
                series=EIA_SYSTEM_LOAD_SERIES,
                ts=ts,
                value=value,
                unit="mw",
                quality="ESTIMATED",
                recorded_at=recorded_at,
            )
        )
    return obs


def _parse_eia_period(period: str) -> datetime:
    """EIA hourly periods look like `2026-09-26T18` (no minutes/seconds, no timezone -- UTC)."""
    return datetime.fromisoformat(f"{period}:00:00").replace(tzinfo=UTC)


NWS_PRODUCT = "nws-hourly"


def nws_forecast_to_feed_obs(payload: dict[str, object], *, recorded_at: datetime) -> list[FeedObs]:
    """NWS hourly forecast (02b S2.4): the nearest (first) period's temperature and dewpoint, plus sky
    cover *when the response actually reports it*.

    Live defect: `properties.periods[].skyCover` was required, but a live `/gridpoints/{office}/{x},{y}
    /forecast/hourly` response (confirmed against grid EWX/156,91) never carries that field at all --
    it only appears on the raw `/gridpoints/{office}/{x},{y}` time-series product, a different payload
    shape entirely (a `{values: [{validTime, value}, ...]}` series, not a plain number on a period).
    Requiring it made every real response fail as `FeedDataError`, so og-feeds' NWS feed never recorded
    a single success. Temperature and dewpoint (which this endpoint does document) are still returned
    every time; `sky_cover` is only added when the field is present, so a future response that does
    include it is still captured (K7: degrade, don't trip -- one missing optional field must not drop
    the whole observation)."""
    properties = payload.get("properties")
    if not isinstance(properties, dict) or not isinstance(properties.get("periods"), list):
        raise FeedDataError("NWS response missing properties.periods")
    periods = properties["periods"]
    if not periods:
        raise FeedDataError("NWS response has an empty periods list")

    period = periods[0]
    try:
        ts = _parse_utc(str(period["startTime"]))
        temp_f = float(period["temperature"])
        dewpoint_c = float(period["dewpoint"]["value"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FeedDataError(f"NWS period malformed: {period!r}") from exc

    obs = [
        FeedObs(
            source=SOURCE_NWS,
            product=NWS_PRODUCT,
            series="temperature",
            ts=ts,
            value=(temp_f - 32.0) * 5.0 / 9.0,
            unit="degc",
            quality="GOOD",
            recorded_at=recorded_at,
        ),
        FeedObs(
            source=SOURCE_NWS,
            product=NWS_PRODUCT,
            series="dewpoint",
            ts=ts,
            value=dewpoint_c,
            unit="degc",
            quality="GOOD",
            recorded_at=recorded_at,
        ),
    ]
    sky_cover = period.get("skyCover")
    if isinstance(sky_cover, int | float) and not isinstance(sky_cover, bool):
        obs.append(
            FeedObs(
                source=SOURCE_NWS,
                product=NWS_PRODUCT,
                series="sky_cover",
                ts=ts,
                value=float(sky_cover),
                unit="pct",
                quality="GOOD",
                recorded_at=recorded_at,
            )
        )
    return obs
