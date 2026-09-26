"""Unit tests for `opengrid.invariants.checks`' pure violation-detection logic (00-invariants.md K1,
K2, K13, orphan reservations/commitments): each check must detect a seeded violation and report none on
clean data (BUILD.md task brief's own acceptance bar for these tests)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from opengrid.invariants import checks

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


# --- K1: reserve breach ------------------------------------------------------------------------------


def test_find_reserve_breaches_detects_discharge_below_reserve() -> None:
    rows = [("hub-1", NOW, 1.5, -5.0, 2.0)]  # soc 1.5 < reserve 2.0, discharging
    violations = checks.find_reserve_breaches(rows)
    assert len(violations) == 1
    assert violations[0].scope == {"hub_id": "hub-1", "ts": NOW.isoformat()}
    assert violations[0].detail["soc_kwh"] == 1.5


def test_find_reserve_breaches_clean_when_above_reserve() -> None:
    rows = [("hub-1", NOW, 5.0, -5.0, 2.0)]  # soc above reserve
    assert checks.find_reserve_breaches(rows) == []


def test_find_reserve_breaches_clean_when_idle_or_charging_below_reserve() -> None:
    """Sitting below reserve while NOT discharging (idle, or charging back up) is not itself a K1
    breach -- see the function's own docstring."""
    rows = [
        ("hub-1", NOW, 1.0, 0.0, 2.0),  # idle
        ("hub-2", NOW, 1.0, 3.0, 2.0),  # charging
    ]
    assert checks.find_reserve_breaches(rows) == []


# --- K2: double-sold kWh -----------------------------------------------------------------------------


def test_find_double_sold_detects_over_capacity_bank_interval() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    rows = [("bank-000", start, end, 650.0, 600.0)]  # 50 kW over a 600 kVA bank
    violations = checks.find_double_sold(rows)
    assert len(violations) == 1
    assert violations[0].detail["excess_kw"] == 50.0
    # 50 kW excess over a 15-minute (0.25h) interval = 12.5 kWh sold twice.
    assert violations[0].magnitude == 12.5


def test_find_double_sold_clean_when_within_capacity() -> None:
    rows = [("bank-000", NOW, NOW + timedelta(minutes=15), 550.0, 600.0)]
    assert checks.find_double_sold(rows) == []


# --- K13: commitment-lock violation ------------------------------------------------------------------


def test_find_lock_violations_detects_dip_without_allowed_reason() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    rows = [("ob-1", start, end, 100.0, 40.0, False)]  # granted well below committed, no override
    violations = checks.find_lock_violations(rows)
    assert len(violations) == 1
    assert violations[0].scope == {"obligation_id": "ob-1", "interval_start": start.isoformat()}
    assert violations[0].detail["committed_kw"] == 100.0
    assert violations[0].detail["min_granted_kw"] == 40.0


def test_find_lock_violations_clean_when_allowed_reason_present() -> None:
    rows = [("ob-1", NOW, NOW + timedelta(minutes=15), 100.0, 40.0, True)]
    assert checks.find_lock_violations(rows) == []


def test_find_lock_violations_clean_when_not_dipped() -> None:
    rows = [("ob-1", NOW, NOW + timedelta(minutes=15), 100.0, 100.0, False)]
    assert checks.find_lock_violations(rows) == []


# --- orphan reservations / commitments ----------------------------------------------------------------


def test_find_orphan_reservations_wraps_every_prefiltered_row() -> None:
    rows = [("res-1", "ob-1", "bank-000", NOW)]
    violations = checks.find_orphan_reservations(rows)
    assert len(violations) == 1
    assert violations[0].scope == {"reservation_id": "res-1", "obligation_id": "ob-1", "bank_id": "bank-000"}


def test_find_orphan_reservations_clean_on_no_rows() -> None:
    assert checks.find_orphan_reservations([]) == []


def test_find_orphan_commitments_wraps_every_prefiltered_row() -> None:
    rows = [("com-1", "ob-1", NOW, "REJECTED")]
    violations = checks.find_orphan_commitments(rows)
    assert len(violations) == 1
    assert violations[0].detail["obligation_state"] == "REJECTED"


def test_find_orphan_commitments_clean_on_no_rows() -> None:
    assert checks.find_orphan_commitments([]) == []
