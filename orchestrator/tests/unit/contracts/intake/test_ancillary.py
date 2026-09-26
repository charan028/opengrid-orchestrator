"""Hand-computed value tests for `opengrid.contracts.intake.ancillary` (ERCOT_AS capacity-hold, task
brief: "value MCPC x MW", "min 0.1 MW, 0.1 increment")."""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from opengrid.contracts.intake.ancillary import OPERATING_DAY_HOURS, compute_as_candidates
from opengrid.core.products import ProductRule

# ERCOT_AS product rule from the demo seed (0002_seed_demo.sql): min 0.1 MW / 0.1 MW increment.
AS_RULE = ProductRule(min_qty_kw=Decimal("100"), increment_kw=Decimal("100"), block=False)

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)


def test_one_candidate_per_operating_day_hour_at_mcpc_value() -> None:
    candidates = compute_as_candidates(now=NOW, mcpc_usd_per_mwh=15.5, rule=AS_RULE, offer_kw=Decimal("500"))
    assert len(candidates) == OPERATING_DAY_HOURS
    assert all(c.value_per_mwh == Decimal("15.5") for c in candidates)
    # offer_kw=500 rounds down to a 100 kW multiple under the rule -> 500 (already a multiple).
    assert all(c.requested_kw == Decimal("500") for c in candidates)
    # consecutive hourly windows.
    for i, candidate in enumerate(candidates):
        assert (candidate.window_end - candidate.window_start).total_seconds() == 3600
        if i > 0:
            assert candidate.window_start == candidates[i - 1].window_end


def test_offer_below_min_qty_yields_no_candidates() -> None:
    candidates = compute_as_candidates(now=NOW, mcpc_usd_per_mwh=15.5, rule=AS_RULE, offer_kw=Decimal("50"))
    assert candidates == []


def test_offer_between_increments_rounds_down() -> None:
    # 250 kW with min 100 / increment 100 -> rounds down to 200 kW (100 + 1*100).
    candidates = compute_as_candidates(now=NOW, mcpc_usd_per_mwh=15.5, rule=AS_RULE, offer_kw=Decimal("250"))
    assert all(c.requested_kw == Decimal("200") for c in candidates)


def test_candidates_start_at_next_operating_day() -> None:
    candidates = compute_as_candidates(now=NOW, mcpc_usd_per_mwh=15.5, rule=AS_RULE, offer_kw=Decimal("500"))
    first_start_local = candidates[0].window_start
    assert first_start_local > NOW
    assert (first_start_local - NOW).total_seconds() > 3600 * 8  # tomorrow, not later today
