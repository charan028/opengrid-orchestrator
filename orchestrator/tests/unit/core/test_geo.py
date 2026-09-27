"""`opengrid.core.geo`: the one D-31 at-home-station rule (G-35 and the selector both use it)."""

from __future__ import annotations

import pytest

from opengrid.core import geo

DEPOT = (32.8385, -96.9730)


def test_distance_km_is_the_great_circle_distance():
    assert geo.distance_km(DEPOT, DEPOT) == 0.0
    # Dallas to Austin is ~293 km as the crow flies.
    assert geo.distance_km((32.7767, -96.7970), (30.2672, -97.7431)) == pytest.approx(293.0, abs=5.0)


@pytest.mark.parametrize(
    ("position", "site", "expected"),
    [
        ((32.8386, -96.9731), DEPOT, True),  # ~15 m: in the depot yard
        ((32.8405, -96.9730), DEPOT, True),  # ~222 m: still inside 250 m
        ((32.8410, -96.9730), DEPOT, False),  # ~278 m: outside
        ((29.4241, -98.4936), DEPOT, False),  # San Antonio
        (None, DEPOT, None),  # no recorded position
        (DEPOT, None, None),  # no station coordinates
    ],
)
def test_at_home_station(position, site, expected):
    assert geo.at_home_station(position, site) is expected
