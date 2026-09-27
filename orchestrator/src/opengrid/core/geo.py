"""The ONE D-31 "at its home station" rule for mobile storage units (trucks), shared by the guardian's
G-35 check and the selector's charge planning, so both judge a truck's position the same way.

A unit is at its home station when its recorded position (`og.hub.lat/lon`, kept current by the device-
info intake) is within `HOME_STATION_RADIUS_KM` of the station's registry coordinates. Pure; no I/O.
"""

from __future__ import annotations

import math

#: ASSUMPTION: 250 m covers a depot yard plus GPS error.
HOME_STATION_RADIUS_KM = 0.25
_EARTH_RADIUS_KM = 6371.0088

LatLon = tuple[float, float]


def distance_km(a: LatLon, b: LatLon) -> float:
    """Great-circle (haversine) distance between two (lat, lon) points in degrees."""
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * _EARTH_RADIUS_KM * math.asin(min(1.0, math.sqrt(h)))


def at_home_station(
    position: LatLon | None, site: LatLon | None, *, radius_km: float = HOME_STATION_RADIUS_KM
) -> bool | None:
    """True within `radius_km` of the home station, False farther away, None when either the position or
    the station is unknown (callers treat unknown as away: fail closed)."""
    if position is None or site is None:
        return None
    return distance_km(position, site) <= radius_km
