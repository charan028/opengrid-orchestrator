"""Normalization of raw ERCOT/EIA/NWS payloads into `opengrid.core.models.platform.FeedObs` rows
(02b S2.7).

Pure functions: no I/O, no network, no clock reads except a passed-in `recorded_at`, so they are unit
tested with fixture payloads (`interfaces/http/market-api.md` shapes) alone. Each function raises
`FeedDataError` on a malformed payload -- `feeds.ercot`/`eia`/`nws` decide whether that counts as a
breaker failure.
"""

from __future__ import annotations

from datetime import UTC, datetime

from opengrid.core.models.platform import FeedObs

SOURCE_ERCOT = "ERCOT"
SOURCE_EIA = "EIA"
SOURCE_NWS = "NWS"

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


def ercot_spp_to_feed_obs(
    payload: dict[str, object], *, product: str, recorded_at: datetime
) -> list[FeedObs]:
    """NP6-905-CD settlement point price ($/MWh) per load zone."""
    rows = normalize_ercot_envelope(
        payload,
        product=product,
        columns={"ts": "deliveryDateTime", "series": "settlementPoint", "value": "settlementPointPrice"},
        recorded_at=recorded_at,
    )
    return [
        FeedObs(
            source=SOURCE_ERCOT,
            product=product,
            series=str(r["series"]),
            ts=_parse_utc(str(r["ts"])),
            value=float(str(r["value"])),
            unit="usd_per_mwh",
            quality="GOOD",
            recorded_at=recorded_at,
        )
        for r in rows
    ]


def ercot_load_to_feed_obs(
    payload: dict[str, object], *, product: str, recorded_at: datetime
) -> list[FeedObs]:
    """NP6-345-CD actual system load (MW) per weather zone."""
    rows = normalize_ercot_envelope(
        payload,
        product=product,
        columns={"ts": "operatingDateTime", "series": "weatherZone", "value": "load"},
        recorded_at=recorded_at,
    )
    return [
        FeedObs(
            source=SOURCE_ERCOT,
            product=product,
            series=str(r["series"]),
            ts=_parse_utc(str(r["ts"])),
            value=float(str(r["value"])),
            unit="mw",
            quality="GOOD",
            recorded_at=recorded_at,
        )
        for r in rows
    ]


def ercot_wind_to_feed_obs(
    payload: dict[str, object], *, product: str, recorded_at: datetime
) -> list[FeedObs]:
    """NP4-732-CD system-wide wind actual + forecast (MW), stored as two series: `actual`, `forecast`."""
    return _ercot_actual_forecast_to_feed_obs(
        payload,
        product=product,
        ts_col="postedDatetime",
        actual_col="actualSystemWideWindOutput",
        forecast_col="windOutputForecastSystemWide",
        recorded_at=recorded_at,
    )


def ercot_solar_to_feed_obs(
    payload: dict[str, object], *, product: str, recorded_at: datetime
) -> list[FeedObs]:
    """NP4-737-CD system-wide solar actual + forecast (MW), stored as two series: `actual`, `forecast`."""
    return _ercot_actual_forecast_to_feed_obs(
        payload,
        product=product,
        ts_col="postedDatetime",
        actual_col="actualSystemWideSolarOutput",
        forecast_col="solarOutputForecastSystemWide",
        recorded_at=recorded_at,
    )


def _ercot_actual_forecast_to_feed_obs(
    payload: dict[str, object],
    *,
    product: str,
    ts_col: str,
    actual_col: str,
    forecast_col: str,
    recorded_at: datetime,
) -> list[FeedObs]:
    rows = normalize_ercot_envelope(
        payload,
        product=product,
        columns={"ts": ts_col, "actual": actual_col, "forecast": forecast_col},
        recorded_at=recorded_at,
    )
    obs: list[FeedObs] = []
    for r in rows:
        ts = _parse_utc(str(r["ts"]))
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
    """NP4-188-CD day-ahead AS clearing price ($/MW-h), one series per ancillary product."""
    fields = payload.get("fields")
    rows = payload.get("data")
    if not isinstance(fields, list) or not isinstance(rows, list):
        raise FeedDataError(f"{product}: malformed envelope, missing fields/data")

    date_idx = _field_index(fields, "deliveryDate")
    hour_idx = _field_index(fields, "hourEnding")
    type_idx = _field_index(fields, "ancillaryType")
    price_idx = _field_index(fields, "mcpc")
    max_index = max(date_idx, hour_idx, type_idx, price_idx)

    obs: list[FeedObs] = []
    for row in rows:
        if not isinstance(row, list) or len(row) <= max_index:
            raise FeedDataError(f"{product}: data row has wrong arity: {row!r}")
        ts = _parse_delivery_date_hour_ending(str(row[date_idx]), str(row[hour_idx]))
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


def _parse_delivery_date_hour_ending(delivery_date: str, hour_ending: str) -> datetime:
    """`deliveryDate` is a calendar date, `hourEnding` an ERCOT "HH:MM" hour-ending label; combine into
    the UTC instant at the *start* of that hour (hour-ending N means the interval ending at N)."""
    date_part = delivery_date.split("T")[0]
    hour = int(hour_ending.split(":")[0]) - 1
    naive = datetime.fromisoformat(f"{date_part}T00:00:00").replace(tzinfo=UTC)
    return naive.replace(hour=hour % 24)


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
    """NWS hourly forecast (02b S2.4): the nearest (first) period's temperature, dewpoint, and sky
    cover, as three series under one product."""
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
        sky_cover_pct = float(period["skyCover"])
    except (KeyError, TypeError, ValueError) as exc:
        raise FeedDataError(f"NWS period malformed: {period!r}") from exc

    return [
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
        FeedObs(
            source=SOURCE_NWS,
            product=NWS_PRODUCT,
            series="sky_cover",
            ts=ts,
            value=sky_cover_pct,
            unit="pct",
            quality="GOOD",
            recorded_at=recorded_at,
        ),
    ]
