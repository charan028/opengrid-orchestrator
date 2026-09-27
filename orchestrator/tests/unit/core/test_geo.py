"""`opengrid.core.geo`: the one D-31 at-home-station rule (G-35 and the selector both use it)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

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


NOW = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)


def test_fresh_positions_keeps_only_recent_device_reports_keyed_by_hub_and_bank():
    rows = [
        ("truck-a", "bank-truck-a", 32.8385, -96.9730, NOW - timedelta(seconds=30)),  # fresh
        (
            "truck-b",
            "bank-truck-b",
            29.4241,
            -98.4936,
            NOW - timedelta(seconds=geo.MOBILE_POSITION_MAX_AGE_S + 1),
        ),
        ("truck-c", "bank-truck-c", None, None, None),  # never reported
        ("truck-d", "bank-truck-d", 30.0, -97.0, None),  # no report time: unknown
        ("truck-e", "bank-truck-e", 30.0, -97.0, NOW + timedelta(minutes=10)),  # future-stamped: rejected
    ]
    assert geo.fresh_positions(rows, NOW) == {"truck-a": DEPOT, "bank-truck-a": DEPOT}
    assert "truck-b" in geo.fresh_positions(rows, NOW, max_age_s=3600.0)


def test_the_shared_query_reads_the_device_reported_position_never_the_seed():
    sql = geo.DEVICE_POSITIONS_SQL
    assert "device_lat" in sql and "device_lon" in sql and "device_info_at" in sql
    assert " lat," not in sql and " lon," not in sql
