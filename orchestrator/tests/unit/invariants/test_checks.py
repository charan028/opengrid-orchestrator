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


def test_find_lock_violations_detects_dip_without_covering_reason() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    dip_at = start + timedelta(minutes=5)
    rows = [("ob-1", start, end, 100.0, 40.0, dip_at, None)]  # no covering trace at all
    violations = checks.find_lock_violations(rows)
    assert len(violations) == 1
    assert violations[0].scope == {"obligation_id": "ob-1", "interval_start": start.isoformat()}
    assert violations[0].detail["committed_kw"] == 100.0
    assert violations[0].detail["dip_kw"] == 40.0


def test_find_lock_violations_clean_when_covering_reason_at_or_before_dip() -> None:
    start = NOW
    dip_at = start + timedelta(minutes=5)
    rows = [("ob-1", start, start + timedelta(minutes=15), 100.0, 40.0, dip_at, dip_at)]
    assert checks.find_lock_violations(rows) == []


def test_find_lock_violations_flagged_when_covering_reason_after_dip() -> None:
    """A SHORTFALL traced AFTER the dip does not retroactively excuse it (checks._dip_is_covered)."""
    start = NOW
    dip_at = start + timedelta(minutes=5)
    covering_at = start + timedelta(minutes=6)
    rows = [("ob-1", start, start + timedelta(minutes=15), 100.0, 40.0, dip_at, covering_at)]
    assert len(checks.find_lock_violations(rows)) == 1


# --- K13: find_dip (worst delivery point, including implicit-zero gaps) -------------------------------


def test_find_dip_none_when_never_below_committed() -> None:
    # A window no wider than the cycles' own coverage plus max_gap_s -- no implicit-zero gap forms,
    # isolating the "did any recorded cycle dip" question from the separate gap-detection behaviour.
    start = NOW
    end = start + timedelta(seconds=6)
    cycles = [(start + timedelta(seconds=2), 100.0), (start + timedelta(seconds=4), 100.0)]
    assert checks.find_dip(interval_start=start, interval_end=end, committed_kw=100.0, cycles=cycles) is None


def test_find_dip_detects_low_cycle_total() -> None:
    start = NOW
    end = start + timedelta(seconds=8)
    low_at = start + timedelta(seconds=4)
    cycles = [(start + timedelta(seconds=2), 100.0), (low_at, 40.0), (start + timedelta(seconds=6), 100.0)]
    dip = checks.find_dip(interval_start=start, interval_end=end, committed_kw=100.0, cycles=cycles)
    assert dip == (40.0, low_at)


def test_find_dip_sums_grants_across_banks_in_the_same_cycle() -> None:
    """The bug the lead reported: an obligation whose banks summed EXACTLY to its commitment must not be
    flagged. `fetch_grant_cycle_series` sums across banks before `find_dip` ever sees a row, so a single
    combined sample equal to the commitment is clean."""
    start = NOW
    end = start + timedelta(seconds=4)
    cycles = [(start + timedelta(seconds=2), 500.0)]  # already summed across every bank for that cycle
    assert checks.find_dip(interval_start=start, interval_end=end, committed_kw=500.0, cycles=cycles) is None


def test_find_dip_detects_gap_with_no_grant_activity_at_all() -> None:
    """The most important case: a stretch with literally no grant row is worse than any recorded low
    value, and must not be invisible to a plain MIN() over only the cycles that DID report."""
    start = NOW
    end = NOW + timedelta(minutes=15)
    # Grants for the first minute, then total silence for the rest of the 15-minute window.
    cycles = [(start + timedelta(seconds=2), 100.0), (start + timedelta(seconds=4), 100.0)]
    dip = checks.find_dip(
        interval_start=start, interval_end=end, committed_kw=100.0, cycles=cycles, max_gap_s=30.0
    )
    assert dip is not None
    dip_kw, dip_at = dip
    assert dip_kw == 0.0
    assert dip_at == start + timedelta(seconds=4)  # the gap starts right after the last real sample


def test_find_dip_detects_gap_when_there_are_no_cycles_at_all() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    dip = checks.find_dip(
        interval_start=start, interval_end=end, committed_kw=100.0, cycles=[], max_gap_s=30.0
    )
    assert dip == (0.0, start)


def test_find_dip_none_for_a_short_window_with_no_cycles_yet() -> None:
    """A just-committed interval shorter than the gap threshold hasn't had time to prove anything either
    way -- not enough elapsed silence to call it a gap yet."""
    start = NOW
    end = NOW + timedelta(seconds=10)
    assert (
        checks.find_dip(interval_start=start, interval_end=end, committed_kw=100.0, cycles=[], max_gap_s=30.0)
        is None
    )


# --- K13: _dip_is_covered (the single-source coverage policy) ------------------------------------------


def test_dip_is_covered_policy_directly() -> None:
    dip_at = NOW
    assert checks._dip_is_covered(dip_at, None) is False
    assert checks._dip_is_covered(dip_at, dip_at) is True  # covering trace at the same instant counts
    assert checks._dip_is_covered(dip_at, dip_at - timedelta(seconds=1)) is True  # before the dip
    assert checks._dip_is_covered(dip_at, dip_at + timedelta(seconds=1)) is False  # after the dip


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
