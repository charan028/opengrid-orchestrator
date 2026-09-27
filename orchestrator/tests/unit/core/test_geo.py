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
    old = NOW - timedelta(seconds=geo.MOBILE_POSITION_MAX_AGE_S + 1)
    rows = [
        ("truck-a", "bank-truck-a", 32.8385, -96.9730, NOW - timedelta(seconds=30), None),  # fresh report
        ("truck-b", "bank-truck-b", 29.4241, -98.4936, old, None),  # old report, no telemetry
        ("truck-c", "bank-truck-c", None, None, None, NOW),  # never reported a position
        ("truck-d", "bank-truck-d", 30.0, -97.0, None, NOW),  # no report time: unknown
        ("truck-e", "bank-truck-e", 30.0, -97.0, NOW + timedelta(minutes=10), NOW),  # future-stamped
    ]
    assert geo.fresh_positions(rows, NOW) == {"truck-a": DEPOT, "bank-truck-a": DEPOT}
    assert "truck-b" in geo.fresh_positions(rows, NOW, max_age_s=3600.0)
    # The stationary rule never rescues a unit with no position, no report time or a future stamp.
    assert set(geo.fresh_positions(rows, NOW, telemetry_max_age_s=25.0)) == {"truck-a", "bank-truck-a"}


# --- Stationary rule (r3.4.2 review MEDIUM): a real truck reports its position on connect and on change
# only; parked, it keeps its last reported position while its telemetry is fresh --------------------------

TWENTY_MIN_AGO = NOW - timedelta(minutes=20)


def _row(position, reported_at, last_seen_at):
    return ("truck-a", "bank-truck-a", *position, reported_at, last_seen_at)


def test_a_parked_truck_with_fresh_telemetry_keeps_a_20_minute_old_position():
    rows = [_row(DEPOT, TWENTY_MIN_AGO, NOW - timedelta(seconds=8))]
    positions = geo.fresh_positions(rows, NOW, telemetry_max_age_s=25.0)
    assert positions["truck-a"] == DEPOT
    assert geo.at_home_station(positions["truck-a"], DEPOT) is True


def test_stale_or_missing_telemetry_makes_an_old_position_unknown():
    stale = [_row(DEPOT, TWENTY_MIN_AGO, NOW - timedelta(seconds=26))]
    assert geo.fresh_positions(stale, NOW, telemetry_max_age_s=25.0) == {}
    never = [_row(DEPOT, TWENTY_MIN_AGO, None)]
    assert geo.fresh_positions(never, NOW, telemetry_max_age_s=25.0) == {}
    # Without the telemetry rule (no threshold given) only the report age counts.
    assert geo.fresh_positions([_row(DEPOT, TWENTY_MIN_AGO, NOW)], NOW) == {}
    unknown = geo.fresh_positions(stale, NOW, telemetry_max_age_s=25.0).get("truck-a")
    assert geo.at_home_station(unknown, DEPOT) is None  # unknown: G-35 and the selector treat it as away


def test_a_move_is_a_new_report_and_the_truck_is_away():
    site = (32.7767, -96.7970)
    rows = [_row(site, NOW - timedelta(seconds=3), NOW - timedelta(seconds=2))]
    positions = geo.fresh_positions(rows, NOW, telemetry_max_age_s=25.0)
    assert positions["truck-a"] == site
    assert geo.at_home_station(positions["truck-a"], DEPOT) is False


def test_the_shared_query_reads_the_device_reported_position_never_the_seed():
    sql = geo.DEVICE_POSITIONS_SQL
    assert "device_lat" in sql and "device_lon" in sql and "device_info_at" in sql
    assert "hs.last_seen_at" in sql  # telemetry freshness for the stationary rule
    assert " lat," not in sql and " lon," not in sql
