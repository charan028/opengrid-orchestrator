"""Unit tests for `opengrid.invariants.checks`' pure violation-detection logic (00-invariants.md K1,
K2, K13, orphan reservations/commitments): each check must detect a seeded violation and report none on
clean data (BUILD.md task brief's own acceptance bar for these tests)."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from opengrid.invariants import checks

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


# --- K1: reserve breach ------------------------------------------------------------------------------


def test_find_reserve_breaches_detects_discharge_below_reserve() -> None:
    rows = [("hub-1", NOW, 1.5, -5.0, 2.0)]  # soc 1.5 < reserve 2.0, discharging
    violations = checks.find_reserve_breaches(rows)
    assert len(violations) == 1
    assert violations[0].scope == {"hub_id": "hub-1", "ts": NOW.isoformat()}
    assert violations[0].detail["soc_kwh"] == 1.5
    assert violations[0].dedupe_key == f"hub-1|{NOW.isoformat()}"


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


# --- K2: double-sold kWh (capability, not nameplate) --------------------------------------------------


def test_find_double_sold_detects_over_true_capacity_bank_interval() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    rows = [("bank-000", start, end, 650.0)]  # 50 kW over the bank's TRUE (admitted) capability
    violations = checks.find_double_sold(rows, capability_by_bank={"bank-000": 600.0})
    assert len(violations) == 1
    assert violations[0].detail["excess_kw"] == 50.0
    assert violations[0].dedupe_key == f"bank-000|{start.isoformat()}"
    # 50 kW excess over a 15-minute (0.25h) interval = 12.5 kWh sold twice.
    assert violations[0].magnitude == 12.5


def test_find_double_sold_clean_when_within_true_capacity() -> None:
    rows = [("bank-000", NOW, NOW + timedelta(minutes=15), 550.0)]
    assert checks.find_double_sold(rows, capability_by_bank={"bank-000": 600.0}) == []


def test_find_double_sold_detects_oversale_below_nameplate_but_above_true_capability() -> None:
    """The bug the lead reported: nameplate (kva_rating) always overstates the real ceiling once any
    hub is offline/low-SoC -- comparing against nameplate misses a real oversale sitting below it."""
    rows = [("bank-000", NOW, NOW + timedelta(minutes=15), 400.0)]  # well under a 600 kVA nameplate
    # True admitted capability (half the fleet offline, say) is only 300 kW.
    violations = checks.find_double_sold(rows, capability_by_bank={"bank-000": 300.0})
    assert len(violations) == 1
    assert violations[0].detail["excess_kw"] == 100.0


def test_find_double_sold_treats_bank_with_no_capability_row_as_zero_capacity() -> None:
    rows = [("bank-999", NOW, NOW + timedelta(minutes=15), 1.0)]
    violations = checks.find_double_sold(rows, capability_by_bank={})
    assert len(violations) == 1
    assert violations[0].detail["capability_kw"] == 0.0


def test_compute_bank_capabilities_kw_excludes_offline_hubs_and_caps_at_bank_rating() -> None:
    hub_rows = [
        # (bank_id, kva_rating, reserve_kva, e_kwh, r_kwh, p_kw, eta_c, eta_d, soc_kwh, health, units)
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 39.2, "online", 1),
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 39.2, "online", 1),
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 39.2, "offline", 1),  # excluded
    ]
    result = checks.compute_bank_capabilities_kw(hub_rows)
    # Two online hubs, each can discharge up to their p_kw (11.0): 22.0 kW, well under the 600 kW cap.
    assert result["bank-000"] == 22.0


def test_compute_bank_capabilities_kw_applies_the_unit_cap() -> None:
    """K2 units (migration 0032): a dual-unit home counts at 20 kW; a single-unit home mis-seeded at 20 kW
    and a home with an unknown unit count (pre-0032 database) both fail closed to 11 kW."""
    hub_rows: list[tuple[str, float, float, float, float, float, float, float, float, str, int | None]] = [
        ("bank-000", 600.0, 0.0, 78.4, 15.68, 20.0, 0.9487, 0.9487, 78.4, "online", 2),
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 20.0, 0.9487, 0.9487, 39.2, "online", 1),
        ("bank-000", 600.0, 0.0, 78.4, 15.68, 20.0, 0.9487, 0.9487, 78.4, "online", None),
    ]
    result = checks.compute_bank_capabilities_kw(hub_rows)
    assert result["bank-000"] == pytest.approx(20.0 + 11.0 + 11.0)


def test_compute_bank_capabilities_kw_includes_bank_with_all_hubs_excluded() -> None:
    hub_rows = [("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 1.0, "fault", 1)]
    result = checks.compute_bank_capabilities_kw(hub_rows)
    assert result["bank-000"] == 0.0


# --- K13: find_dip (worst delivery point, including implicit-zero gaps) -------------------------------


def test_find_dip_none_when_never_below_committed() -> None:
    # A window no wider than the cycles' own coverage plus max_gap_s -- no implicit-zero gap forms,
    # isolating the "did any recorded cycle dip" question from the separate gap-detection behaviour.
    start = NOW
    end = start + timedelta(seconds=6)
    cycles = [(start + timedelta(seconds=2), 100.0, "cyc-1"), (start + timedelta(seconds=4), 100.0, "cyc-2")]
    assert checks.find_dip(interval_start=start, interval_end=end, committed_kw=100.0, cycles=cycles) is None


def test_find_dip_detects_low_cycle_total() -> None:
    start = NOW
    end = start + timedelta(seconds=8)
    low_at = start + timedelta(seconds=4)
    cycles = [
        (start + timedelta(seconds=2), 100.0, "cyc-1"),
        (low_at, 40.0, "cyc-2"),
        (start + timedelta(seconds=6), 100.0, "cyc-3"),
    ]
    dip = checks.find_dip(interval_start=start, interval_end=end, committed_kw=100.0, cycles=cycles)
    assert dip is not None
    assert dip.kw == 40.0
    assert dip.at == low_at
    assert dip.cycle_id == "cyc-2"
    assert dip.is_gap is False


def test_find_dip_sums_grants_across_banks_in_the_same_cycle() -> None:
    """The bug the lead reported: an obligation whose banks summed EXACTLY to its commitment must not be
    flagged. `fetch_grant_cycle_series` sums across banks before `find_dip` ever sees a row, so a single
    combined sample equal to the commitment is clean."""
    start = NOW
    end = start + timedelta(seconds=4)
    cycles = [(start + timedelta(seconds=2), 500.0, "cyc-1")]  # already summed across every bank
    assert checks.find_dip(interval_start=start, interval_end=end, committed_kw=500.0, cycles=cycles) is None


def test_find_dip_detects_gap_with_no_grant_activity_at_all() -> None:
    """The most important case: a stretch with literally no grant row is worse than any recorded low
    value, and must not be invisible to a plain MIN() over only the cycles that DID report."""
    start = NOW
    end = NOW + timedelta(minutes=15)
    # Grants for the first few seconds, then total silence for the rest of the 15-minute window.
    cycles = [(start + timedelta(seconds=2), 100.0, "cyc-1"), (start + timedelta(seconds=4), 100.0, "cyc-2")]
    dip = checks.find_dip(
        interval_start=start, interval_end=end, committed_kw=100.0, cycles=cycles, max_gap_s=30.0
    )
    assert dip is not None
    assert dip.kw == 0.0
    assert dip.at == start + timedelta(seconds=4)  # the gap starts right after the last real sample
    assert dip.cycle_id is None  # a gap is not any one cycle's own reported sample
    assert dip.is_gap is True


def test_find_dip_detects_gap_when_there_are_no_cycles_at_all() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    dip = checks.find_dip(
        interval_start=start, interval_end=end, committed_kw=100.0, cycles=[], max_gap_s=30.0
    )
    assert dip is not None
    assert (dip.kw, dip.at, dip.is_gap) == (0.0, start, True)


def test_find_dip_none_for_a_short_window_with_no_cycles_yet() -> None:
    """A just-committed interval shorter than the gap threshold hasn't had time to prove anything either
    way -- not enough elapsed silence to call it a gap yet."""
    start = NOW
    end = NOW + timedelta(seconds=10)
    assert (
        checks.find_dip(interval_start=start, interval_end=end, committed_kw=100.0, cycles=[], max_gap_s=30.0)
        is None
    )


# --- K13: _dip_is_covered / classify_dip (the single-source coverage + classification policy) ----------


def _dip(*, kw: float = 40.0, at: datetime = NOW, cycle_id: str | None = None, is_gap: bool = False):
    return checks.DipResult(kw=kw, at=at, cycle_id=cycle_id, is_gap=is_gap)


def _covered(
    dip, *, earliest_covering_at, covered_cycle_ids=frozenset(), committed_kw=100.0, shortfall_events=None
):
    return checks._dip_is_covered(
        dip,
        earliest_covering_at=earliest_covering_at,
        covered_cycle_ids=covered_cycle_ids,
        committed_kw=committed_kw,
        shortfall_events=shortfall_events or [],
    )


def test_dip_is_covered_by_timestamp_at_or_before() -> None:
    dip = _dip(at=NOW)
    assert _covered(dip, earliest_covering_at=None) is False
    assert _covered(dip, earliest_covering_at=NOW) is True
    assert _covered(dip, earliest_covering_at=NOW - timedelta(seconds=1)) is True
    assert _covered(dip, earliest_covering_at=NOW + timedelta(seconds=1)) is False


def test_dip_is_covered_by_shortfall_requires_matching_feasible_remainder() -> None:
    """Owner decision 2026-09-26 (best-effort shortfall): a shortfall-sourced cover is compliant only if
    delivery actually reached that moment's feasible remainder, not just any lower value."""
    dip_at = NOW
    shortfall_events = [
        checks.ShortfallEvent(at=NOW - timedelta(seconds=10), shortfall_kw=40.0, cycle_id=None)
    ]
    # committed 100, shortfall 40 -> feasible remainder 60. Delivered exactly 60: compliant.
    dip_ok = _dip(kw=60.0, at=dip_at)
    assert (
        _covered(
            dip_ok,
            earliest_covering_at=NOW - timedelta(seconds=10),
            committed_kw=100.0,
            shortfall_events=shortfall_events,
        )
        is True
    )
    # Delivered only 10 (well under the 60 kW feasible remainder): NOT compliant.
    dip_short = _dip(kw=10.0, at=dip_at)
    assert (
        _covered(
            dip_short,
            earliest_covering_at=NOW - timedelta(seconds=10),
            committed_kw=100.0,
            shortfall_events=shortfall_events,
        )
        is False
    )


def test_dip_is_covered_by_same_cycle_id_epsilon() -> None:
    """The bug the lead reported: the allocator persists a cycle's grant BEFORE it traces that same
    cycle's shortfall, so the covering trace's `created_at` is a few ms AFTER the dip's own timestamp --
    a strict "at or before" timestamp rule alone flags the very first cycle of every legitimate
    exception. Matching by cycle_id sidesteps that write-order race."""
    dip_at = NOW
    covering_at = NOW + timedelta(milliseconds=5)  # trace written moments AFTER the grant, same cycle
    dip = _dip(at=dip_at, cycle_id="cyc-1")
    assert _covered(dip, earliest_covering_at=covering_at, covered_cycle_ids=frozenset({"cyc-1"})) is True
    # A different cycle_id, same late timestamp, is NOT excused by the epsilon -- only by "at or before".
    dip_other_cycle = _dip(at=dip_at, cycle_id="cyc-2")
    assert (
        _covered(dip_other_cycle, earliest_covering_at=covering_at, covered_cycle_ids=frozenset({"cyc-1"}))
        is False
    )


def test_classify_dip_need_basis_is_always_covered() -> None:
    """Owner decision 2026-09-26: a MEASURED_FEEDBACK obligation's committed kW is a reserved maximum,
    not a fixed schedule -- delivery below it (even a total gap, e.g. zero customer need) is compliant
    by definition, with no trace-based coverage check needed at all."""
    lock_shaped = _dip(kw=40.0, at=NOW, cycle_id="cyc-1", is_gap=False)
    gap_shaped = _dip(kw=0.0, at=NOW, cycle_id=None, is_gap=True)
    for dip in (lock_shaped, gap_shaped):
        assert (
            checks.classify_dip(
                dip, earliest_covering_at=None, covered_cycle_ids=frozenset(), is_need_basis=True
            )
            == "covered"
        )


def test_classify_lock_rows_need_basis_obligation_never_flagged() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    rows = [
        (
            "ob-need-basis",
            start,
            end,
            100.0,
            _dip(kw=0.0, at=start + timedelta(minutes=1), cycle_id=None, is_gap=True),
            None,
            frozenset(),
            [],
            True,  # is_need_basis
            False,  # measured_need_is_unmet -- no measured need shown, so compliant
        ),
        (
            "ob-fixed",
            start,
            end,
            100.0,
            _dip(kw=0.0, at=start + timedelta(minutes=1), cycle_id=None, is_gap=True),
            None,
            frozenset(),
            [],
            False,
            False,
        ),
    ]
    lock_violations, outage_gaps = checks.classify_lock_rows(rows)
    assert lock_violations == []
    assert [v.scope["obligation_id"] for v in outage_gaps] == ["ob-fixed"]


def test_classify_lock_rows_need_basis_flagged_when_measured_need_unmet() -> None:
    """K13 need-basis violation (b): a need-basis dip with real measured need and no override IS flagged
    (as a lock_violation or outage_gap, per the usual is_gap split)."""
    start = NOW
    end = NOW + timedelta(minutes=15)
    rows = [
        (
            "ob-need-basis",
            start,
            end,
            100.0,
            _dip(kw=0.0, at=start + timedelta(minutes=1), cycle_id=None, is_gap=True),
            None,
            frozenset(),
            [],
            True,
            True,  # measured_need_is_unmet
        ),
    ]
    lock_violations, outage_gaps = checks.classify_lock_rows(rows)
    assert lock_violations == []
    assert [v.scope["obligation_id"] for v in outage_gaps] == ["ob-need-basis"]


def test_classify_dip_covered_lock_violation_and_outage_gap() -> None:
    covered = _dip(at=NOW, cycle_id="cyc-1")
    assert checks.classify_dip(covered, earliest_covering_at=NOW, covered_cycle_ids=frozenset()) == "covered"

    lock_violation = _dip(at=NOW, cycle_id="cyc-1", is_gap=False)
    assert (
        checks.classify_dip(lock_violation, earliest_covering_at=None, covered_cycle_ids=frozenset())
        == "lock_violation"
    )

    outage_gap = _dip(at=NOW, cycle_id=None, is_gap=True)
    assert (
        checks.classify_dip(outage_gap, earliest_covering_at=None, covered_cycle_ids=frozenset())
        == "outage_gap"
    )


def test_classify_lock_rows_splits_into_lock_violations_and_outage_gaps() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    rows = [
        (
            "ob-lock",
            start,
            end,
            100.0,
            _dip(kw=40.0, at=start + timedelta(minutes=1), cycle_id="cyc-1", is_gap=False),
            None,
            frozenset(),
            [],
            False,
            False,
        ),
        (
            "ob-gap",
            start,
            end,
            100.0,
            _dip(kw=0.0, at=start + timedelta(minutes=2), cycle_id=None, is_gap=True),
            None,
            frozenset(),
            [],
            False,
            False,
        ),
        (
            "ob-covered",
            start,
            end,
            100.0,
            _dip(kw=0.0, at=start + timedelta(minutes=3), cycle_id="cyc-3", is_gap=True),
            start + timedelta(minutes=3),
            frozenset(),
            [],
            False,
            False,
        ),
    ]
    lock_violations, outage_gaps = checks.classify_lock_rows(rows)
    assert [v.scope["obligation_id"] for v in lock_violations] == ["ob-lock"]
    assert [v.scope["obligation_id"] for v in outage_gaps] == ["ob-gap"]


# --- K13 best-effort shortfall / restore-lag ------------------------------------------------------------


def test_feasible_remainder_kw() -> None:
    assert checks.feasible_remainder_kw(100.0, 40.0) == 60.0
    assert checks.feasible_remainder_kw(100.0, 0.0) == 100.0
    assert checks.feasible_remainder_kw(100.0, 150.0) == 0.0  # never negative
    assert checks.feasible_remainder_kw(100.0, -10.0) == 100.0  # a negative shortfall is not a surplus


def test_find_restore_lag_violations_flags_not_restored_within_two_cycles() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    cleared_at = start + timedelta(minutes=1)
    shortfall_events = [
        checks.ShortfallEvent(at=start, shortfall_kw=40.0, cycle_id="cyc-0"),
        checks.ShortfallEvent(at=cleared_at, shortfall_kw=0.0, cycle_id="cyc-1"),  # constraint clears
    ]
    # Two cycles after the clear, still not back to the full 100 kW commitment.
    cycles = [
        (cleared_at + timedelta(seconds=2), 60.0, "cyc-2"),
        (cleared_at + timedelta(seconds=4), 60.0, "cyc-3"),
    ]
    rows = [("ob-1", start, end, 100.0, cycles, shortfall_events)]
    violations = checks.find_restore_lag_violations(rows)
    assert len(violations) == 1
    assert violations[0].scope["obligation_id"] == "ob-1"
    assert violations[0].detail["cleared_at"] == cleared_at.isoformat()


def test_find_restore_lag_violations_clean_when_restored_in_time() -> None:
    start = NOW
    end = NOW + timedelta(minutes=15)
    cleared_at = start + timedelta(minutes=1)
    shortfall_events = [checks.ShortfallEvent(at=cleared_at, shortfall_kw=0.0, cycle_id=None)]
    cycles = [
        (cleared_at + timedelta(seconds=2), 60.0, "cyc-1"),
        (cleared_at + timedelta(seconds=4), 100.0, "cyc-2"),  # restored within 2 cycles
    ]
    rows = [("ob-1", start, end, 100.0, cycles, shortfall_events)]
    assert checks.find_restore_lag_violations(rows) == []


def test_find_restore_lag_violations_waits_for_enough_cycles_to_elapse() -> None:
    """Only one cycle has landed since the clear -- not enough evidence yet, no false positive."""
    start = NOW
    end = NOW + timedelta(minutes=15)
    cleared_at = start + timedelta(minutes=1)
    shortfall_events = [checks.ShortfallEvent(at=cleared_at, shortfall_kw=0.0, cycle_id=None)]
    cycles = [(cleared_at + timedelta(seconds=2), 60.0, "cyc-1")]
    rows = [("ob-1", start, end, 100.0, cycles, shortfall_events)]
    assert checks.find_restore_lag_violations(rows) == []


def test_find_restore_lag_violations_ignores_non_clear_events() -> None:
    """A shortfall event that never reports shortfall_kw==0 (still active) has nothing to restore from."""
    start = NOW
    end = NOW + timedelta(minutes=15)
    shortfall_events = [checks.ShortfallEvent(at=start, shortfall_kw=40.0, cycle_id=None)]
    cycles = [
        (start + timedelta(seconds=2), 60.0, "cyc-1"),
        (start + timedelta(seconds=4), 60.0, "cyc-2"),
    ]
    rows = [("ob-1", start, end, 100.0, cycles, shortfall_events)]
    assert checks.find_restore_lag_violations(rows) == []


# --- K13 need-basis measured-need violation (b) -----------------------------------------------------


def test_measured_need_unmet_none_or_non_good_sample_is_never_a_violation() -> None:
    assert checks.measured_need_unmet(None, delivered_kw=0.0) is False
    suspect = checks.MeasuredNeedSample(
        kind="site_meter", field="p_kw", value=50.0, limit=None, quality="SUSPECT"
    )
    assert checks.measured_need_unmet(suspect, delivered_kw=0.0) is False


def test_measured_need_unmet_data_center_site_meter() -> None:
    # Site still importing 50 kW, only 10 kW delivered -- real unmet need.
    importing = checks.MeasuredNeedSample(
        kind="site_meter", field="p_kw", value=50.0, limit=None, quality="GOOD"
    )
    assert checks.measured_need_unmet(importing, delivered_kw=10.0) is True
    # Site not importing (balanced/exporting) -- no need, the low delivery is expected.
    balanced = checks.MeasuredNeedSample(
        kind="site_meter", field="p_kw", value=0.2, limit=None, quality="GOOD"
    )
    assert checks.measured_need_unmet(balanced, delivered_kw=0.0) is False
    # Delivery already matches/exceeds the import -- served.
    served = checks.MeasuredNeedSample(
        kind="site_meter", field="p_kw", value=50.0, limit=None, quality="GOOD"
    )
    assert checks.measured_need_unmet(served, delivered_kw=50.0) is False


def test_measured_need_unmet_pipeline_ac_corridor() -> None:
    # Induced current at 95% of the corridor limit, nothing delivered -- real unmet need.
    near_limit = checks.MeasuredNeedSample(
        kind="corridor", field="i_ac_a", value=95.0, limit=100.0, quality="GOOD"
    )
    assert checks.measured_need_unmet(near_limit, delivered_kw=0.0) is True
    # Comfortably below the limit -- no real need.
    low = checks.MeasuredNeedSample(kind="corridor", field="i_ac_a", value=20.0, limit=100.0, quality="GOOD")
    assert checks.measured_need_unmet(low, delivered_kw=0.0) is False


# --- orphan reservations / commitments ----------------------------------------------------------------


def test_find_orphan_reservations_wraps_every_prefiltered_row() -> None:
    rows = [("res-1", "ob-1", "bank-000", NOW)]
    violations = checks.find_orphan_reservations(rows)
    assert len(violations) == 1
    assert violations[0].scope == {"reservation_id": "res-1", "obligation_id": "ob-1", "bank_id": "bank-000"}
    assert violations[0].dedupe_key == "res-1"


def test_find_orphan_reservations_clean_on_no_rows() -> None:
    assert checks.find_orphan_reservations([]) == []


def test_find_orphan_commitments_wraps_every_prefiltered_row() -> None:
    rows = [("com-1", "ob-1", NOW, "REJECTED")]
    violations = checks.find_orphan_commitments(rows)
    assert len(violations) == 1
    assert violations[0].detail["obligation_state"] == "REJECTED"
    assert violations[0].dedupe_key == "com-1"


def test_find_orphan_commitments_clean_on_no_rows() -> None:
    assert checks.find_orphan_commitments([]) == []


# --- K15 territory -------------------------------------------------------------------------------------


def test_find_territory_violations_detects_bank_outside_territory() -> None:
    rows = [("grant-1", "ob-1", "bank-000", "LZ_HOUSTON", "AUSTIN_ENERGY", ("LZ_AEN",), NOW)]
    violations = checks.find_territory_violations(rows)
    assert len(violations) == 1
    assert violations[0].scope == {
        "obligation_id": "ob-1",
        "bank_id": "bank-000",
        "utility_id": "AUSTIN_ENERGY",
    }
    assert violations[0].dedupe_key == "ob-1|bank-000"
    assert violations[0].detail["zone"] == "LZ_HOUSTON"


def test_find_territory_violations_clean_when_zone_in_territory() -> None:
    rows = [("grant-1", "ob-1", "bank-000", "LZ_AEN", "AUSTIN_ENERGY", ("LZ_AEN", "LZ_CPS"), NOW)]
    assert checks.find_territory_violations(rows) == []


def test_find_territory_violations_dedupes_repeated_grants_same_obligation_bank() -> None:
    """Same ongoing mismatch across many grant cycles is ONE violation, not one per grant."""
    rows = [
        ("grant-1", "ob-1", "bank-000", "LZ_HOUSTON", "AUSTIN_ENERGY", ("LZ_AEN",), NOW),
        (
            "grant-2",
            "ob-1",
            "bank-000",
            "LZ_HOUSTON",
            "AUSTIN_ENERGY",
            ("LZ_AEN",),
            NOW + timedelta(seconds=2),
        ),
    ]
    violations = checks.find_territory_violations(rows)
    assert {v.dedupe_key for v in violations} == {"ob-1|bank-000"}


# --- AS capacity hold compliance ------------------------------------------------------------------------


def test_find_as_hold_violations_detects_held_energy_below_requirement() -> None:
    # committed 100 kW for 240 min (4h) requires 400 kWh held; only 250 kWh actually held.
    rows = [("dep-1", "ob-1", 100.0, 240, 250.0)]
    violations = checks.find_as_hold_violations(rows)
    assert len(violations) == 1
    assert violations[0].dedupe_key == "dep-1"
    assert violations[0].detail["required_kwh"] == 400.0
    assert violations[0].detail["held_kwh"] == 250.0


def test_find_as_hold_violations_clean_when_held_energy_sufficient() -> None:
    rows = [("dep-1", "ob-1", 100.0, 240, 400.0)]  # exactly at requirement
    assert checks.find_as_hold_violations(rows) == []


# --- Discharge-flow limit (home meter, hub discharge derate, transformer, feeder, substation) -----------


def test_find_flow_limit_violations_detects_home_export_over_meter_limit() -> None:
    rows = [("home_meter", "hub-1", -12.0, None, 10.0)]  # exporting 12 kW against a 10 kW export limit
    violations = checks.find_flow_limit_violations(rows)
    assert len(violations) == 1
    assert violations[0].scope == {"scope_kind": "home_meter", "scope_id": "hub-1", "direction": "reverse"}


def test_find_flow_limit_violations_detects_discharge_over_derated_p_dis_max() -> None:
    # p_dis_max_kw (the BMS's own derated ceiling) is below the hub's nameplate rating.
    rows = [("hub_discharge_derate", "hub-1", -8.0, None, 5.0)]
    violations = checks.find_flow_limit_violations(rows)
    assert len(violations) == 1
    assert violations[0].detail["net_kw"] == -8.0


def test_find_flow_limit_violations_detects_transformer_overload_either_direction() -> None:
    export_over = [("transformer", "xfmr-1", -60.0, 50.0, 50.0)]
    import_over = [("transformer", "xfmr-1", 60.0, 50.0, 50.0)]
    assert [v.scope["direction"] for v in checks.find_flow_limit_violations(export_over)] == ["reverse"]
    assert [v.scope["direction"] for v in checks.find_flow_limit_violations(import_over)] == ["forward"]


def test_find_flow_limit_violations_detects_feeder_asymmetric_limits() -> None:
    # thermal_kw (forward/import) = 100, reverse_kw (export) = 40 -- deliberately asymmetric.
    within_reverse = [("feeder", "feeder-1", -35.0, 100.0, 40.0)]
    over_reverse = [("feeder", "feeder-1", -45.0, 100.0, 40.0)]
    assert checks.find_flow_limit_violations(within_reverse) == []
    assert len(checks.find_flow_limit_violations(over_reverse)) == 1


def test_find_flow_limit_violations_detects_substation_overload() -> None:
    rows = [("substation", "sub-1", -21_000.0, 20_000.0, 20_000.0)]
    violations = checks.find_flow_limit_violations(rows)
    assert len(violations) == 1
    assert violations[0].scope["scope_id"] == "sub-1"


def test_find_flow_limit_violations_clean_within_limits() -> None:
    rows = [("home_meter", "hub-1", -5.0, None, 10.0)]
    assert checks.find_flow_limit_violations(rows) == []


def test_find_flow_limit_violations_skips_when_limit_absent() -> None:
    """Schema-adaptive: a `None` limit means the field is absent for that scope -- never treated as a
    zero ceiling that would flag everything."""
    rows = [("home_meter", "hub-1", -99999.0, None, None)]
    assert checks.find_flow_limit_violations(rows) == []
