"""The ONE D-31 "at its home station" rule for mobile storage units (trucks), shared by the guardian's
G-35 check and the selector's charge planning, so both judge a truck's position the same way.

Position source: the DEVICE-REPORTED position (`og.hub.device_lat/device_lon`, written by the device-info
intake `opengrid.fleet.device_info` from each report, stamped `device_info_at`). Never `og.hub.lat/lon`:
that is the seed (a truck's HOME STATION) and is never moved by a report (H4), so reading it would make
every truck look at home forever.

When is a reported position still the truck's position? (r3.4.2 review MEDIUM.) The device-info contract
(interfaces/mqtt/device_info.schema.json) requires a MOBILE_STORAGE unit to publish on every move and at
least every `MOBILE_POSITION_MAX_AGE_S` (a position heartbeat). A report is trusted:
  * while it is younger than `MOBILE_POSITION_MAX_AGE_S`; or
  * STATIONARY RULE: while the unit's TELEMETRY is fresh (`og.hub_state.last_seen_at` within the caller's
    `telemetry_max_age_s`, `[health].hub_stale_s`). A unit that moves publishes a new report, so a unit
    that is still talking and has reported no move since is still where it last reported -- a real truck
    parked at its depot that only reports on change keeps recharging.
Unknown -- no reported position, or an old report AND stale telemetry, or a report stamped in the future --
is away (fail closed).

`DEVICE_POSITIONS_SQL` is the one query both callers run (each on its own connection, the guardian keeping
its own read); `fresh_positions` turns its rows into positions. A unit is at its home station when its
position is within `HOME_STATION_RADIUS_KM` of the station's registry coordinates.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any

#: ASSUMPTION: 250 m covers a depot yard plus GPS error.
HOME_STATION_RADIUS_KM = 0.25
#: The device-info position heartbeat for MOBILE_STORAGE (interfaces/mqtt/device_info.schema.json): a report
#: younger than this is trusted on its own; an older one only under the stationary rule (fresh telemetry).
MOBILE_POSITION_MAX_AGE_S = 300.0
#: Tolerated clock skew for a report stamped slightly in the future.
_FUTURE_SKEW_S = 5.0
_EARTH_RADIUS_KM = 6371.0088

LatLon = tuple[float, float]

#: The device-reported position of the hubs whose hub id or (single-hub) bank id is in `%(ids)s`, with the
#: hub's last telemetry time (`og.hub_state.last_seen_at`, NULL when it never reported) for the stationary rule.
DEVICE_POSITIONS_SQL = """
SELECT h.hub_id, h.bank_id, h.device_lat, h.device_lon, h.device_info_at, hs.last_seen_at
FROM og.hub h LEFT JOIN og.hub_state hs ON hs.hub_id = h.hub_id
WHERE h.hub_id = ANY(%(ids)s) OR h.bank_id = ANY(%(ids)s)
"""


def distance_km(a: LatLon, b: LatLon) -> float:
    """Great-circle (haversine) distance between two (lat, lon) points in degrees."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def _age_s(now: datetime, at: datetime | None) -> float | None:
    return None if at is None else (now - at).total_seconds()


def fresh_positions(
    rows: Iterable[tuple[Any, ...]],
    now: datetime,
    *,
    max_age_s: float = MOBILE_POSITION_MAX_AGE_S,
    telemetry_max_age_s: float | None = None,
) -> dict[str, LatLon]:
    """`id -> (lat, lon)` from `DEVICE_POSITIONS_SQL` rows, keyed by hub id AND bank id, for every unit whose
    last reported position is still trusted (this module's docstring): the report is younger than
    `max_age_s`, or -- the stationary rule, when `telemetry_max_age_s` is given -- the unit's telemetry is
    younger than `telemetry_max_age_s`. Left out (unknown): no reported position or report time, a report
    stamped in the future, or an old report with stale or missing telemetry."""
    out: dict[str, LatLon] = {}
    for hub_id, bank_id, lat, lon, reported_at, last_seen_at in rows:
        report_age = _age_s(now, reported_at)
        if lat is None or lon is None or report_age is None or report_age < -_FUTURE_SKEW_S:
            continue
        telemetry_age = _age_s(now, last_seen_at)
        stationary = (
            telemetry_max_age_s is not None
            and telemetry_age is not None
            and -_FUTURE_SKEW_S <= telemetry_age <= telemetry_max_age_s
        )
        if report_age <= max_age_s or stationary:
            out[str(hub_id)] = out[str(bank_id)] = (float(lat), float(lon))
    return out


def at_home_station(
    position: LatLon | None, site: LatLon | None, *, radius_km: float = HOME_STATION_RADIUS_KM
) -> bool | None:
    """True within `radius_km` of the home station, False farther away, None when either the position or
    the station is unknown (callers treat unknown as away: fail closed)."""
    if position is None or site is None:
        return None
    return distance_km(position, site) <= radius_km
