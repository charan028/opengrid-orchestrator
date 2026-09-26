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
        # (bank_id, kva_rating, reserve_kva, e_kwh, r_kwh, p_kw, eta_c, eta_d, soc_kwh, health)
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 39.2, "online"),
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 39.2, "online"),
        ("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 39.2, "offline"),  # excluded
    ]
    result = checks.compute_bank_capabilities_kw(hub_rows)
    # Two online hubs, each can discharge up to their p_kw (11.0): 22.0 kW, well under the 600 kW cap.
    assert result["bank-000"] == 22.0


def test_compute_bank_capabilities_kw_includes_bank_with_all_hubs_excluded() -> None:
    hub_rows = [("bank-000", 600.0, 0.0, 39.2, 7.84, 11.0, 0.9487, 0.9487, 1.0, "fault")]
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


def test_dip_is_covered_by_timestamp_at_or_before() -> None:
    dip = _dip(at=NOW)
    assert checks._dip_is_covered(dip, earliest_covering_at=None, covered_cycle_ids=frozenset()) is False
    assert checks._dip_is_covered(dip, earliest_covering_at=NOW, covered_cycle_ids=frozenset()) is True
    assert (
        checks._dip_is_covered(
            dip, earliest_covering_at=NOW - timedelta(seconds=1), covered_cycle_ids=frozenset()
        )
        is True
    )
    assert (
        checks._dip_is_covered(
            dip, earliest_covering_at=NOW + timedelta(seconds=1), covered_cycle_ids=frozenset()
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
    assert (
        checks._dip_is_covered(dip, earliest_covering_at=covering_at, covered_cycle_ids=frozenset({"cyc-1"}))
        is True
    )
    # A different cycle_id, same late timestamp, is NOT excused by the epsilon -- only by "at or before".
    dip_other_cycle = _dip(at=dip_at, cycle_id="cyc-2")
    assert (
        checks._dip_is_covered(
            dip_other_cycle, earliest_covering_at=covering_at, covered_cycle_ids=frozenset({"cyc-1"})
        )
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
            True,  # is_need_basis
        ),
        (
            "ob-fixed",
            start,
            end,
            100.0,
            _dip(kw=0.0, at=start + timedelta(minutes=1), cycle_id=None, is_gap=True),
            None,
            frozenset(),
            False,
        ),
    ]
    lock_violations, outage_gaps = checks.classify_lock_rows(rows)
    assert lock_violations == []
    assert [v.scope["obligation_id"] for v in outage_gaps] == ["ob-fixed"]


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
            False,
        ),
    ]
    lock_violations, outage_gaps = checks.classify_lock_rows(rows)
    assert [v.scope["obligation_id"] for v in lock_violations] == ["ob-lock"]
    assert [v.scope["obligation_id"] for v in outage_gaps] == ["ob-gap"]


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
