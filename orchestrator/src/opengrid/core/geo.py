"""The ONE D-31 "at its home station" rule for mobile storage units (trucks), shared by the guardian's
G-35 check and the selector's charge planning, so both judge a truck's position the same way.

Position source: the DEVICE-REPORTED position (`og.hub.device_lat/device_lon`, written by the device-info
intake `opengrid.fleet.device_info` from each report, stamped `device_info_at`). Never `og.hub.lat/lon`:
that is the seed (a truck's HOME STATION) and is never moved by a report (H4), so reading it would make
every truck look at home forever. A report older than `MOBILE_POSITION_MAX_AGE_S` is stale: the position
is unknown, and unknown is away (fail closed).

`DEVICE_POSITIONS_SQL` is the one query both callers run (each on its own connection, the guardian keeping
its own read); `fresh_positions` turns its rows into positions. A unit is at its home station when its
fresh position is within `HOME_STATION_RADIUS_KM` of the station's registry coordinates.
"""

from __future__ import annotations

import math
from collections.abc import Iterable
from datetime import datetime
from typing import Any

#: ASSUMPTION: 250 m covers a depot yard plus GPS error.
HOME_STATION_RADIUS_KM = 0.25
#: A mobile unit re-reports its position at least every minute (sim: `mobile_position_interval_s`); five
#: minutes without a report means the position is unknown (away, fail closed).
MOBILE_POSITION_MAX_AGE_S = 300.0
_EARTH_RADIUS_KM = 6371.0088

LatLon = tuple[float, float]

#: The device-reported position of the hubs whose hub id or (single-hub) bank id is in `%(ids)s`.
DEVICE_POSITIONS_SQL = """
SELECT hub_id, bank_id, device_lat, device_lon, device_info_at FROM og.hub
WHERE hub_id = ANY(%(ids)s) OR bank_id = ANY(%(ids)s)
"""


def distance_km(a: LatLon, b: LatLon) -> float:
    """Great-circle (haversine) distance between two (lat, lon) points in degrees."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def fresh_positions(
    rows: Iterable[tuple[Any, ...]], now: datetime, *, max_age_s: float = MOBILE_POSITION_MAX_AGE_S
) -> dict[str, LatLon]:
    """`id -> (lat, lon)` from `DEVICE_POSITIONS_SQL` rows, keyed by hub id AND bank id. A row with no
    reported position, no report time, a report older than `max_age_s` or stamped in the future (beyond a
    few seconds of clock skew) is left out: its position is unknown."""
    out: dict[str, LatLon] = {}
    for hub_id, bank_id, lat, lon, reported_at in rows:
        if lat is None or lon is None or reported_at is None:
            continue
        age_s = (now - reported_at).total_seconds()
        if age_s > max_age_s or age_s < -5.0:
            continue
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
