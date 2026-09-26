"""Value tests for `opengrid.contracts.intake.deferral` (DIST_DEFERRAL capacity-hold candidates).

Regression coverage for `qa/merge-notes.md` S15's live finding: intake previously admitted every
DIST_DEFERRAL candidate with `value_per_mwh=None`, which the selector's `og.opportunity.value_per_mwh`
column then persisted as null and `selector.gate.load_candidates` defaulted to `0.0`. Since
`selector.model.build_mode_o_model`'s objective coefficient for a candidate is
`value_per_mwh/1000 - degradation_cost_per_kwh`, a null/zero value made that coefficient strictly
negative (there is always a positive `degradation_cost_per_kwh`), so the LP correctly -- given that
flawed input -- always chose `x_o=0`: 94 `OPTIMAL` solves that never selected the one DIST_DEFERRAL
candidate (0 commitments). Per `02a-mvp-s-spec-engine.md` S1.4, `value_per_mwh` is null only for
`HOME` -- DIST_DEFERRAL must carry a real (positive) value.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from decimal import Decimal

from opengrid.contracts.intake.deferral import (
    DEFAULT_DEFERRAL_VALUE_USD_PER_MWH,
    PEAK_END_HOUR_LOCAL,
    PEAK_START_HOUR_LOCAL,
    compute_deferral_candidates,
)
from opengrid.core.products import ProductRule

# DIST_DEFERRAL product rule from the demo seed (0002_seed_demo.sql): 1 kW granularity.
DEFERRAL_RULE = ProductRule(min_qty_kw=Decimal("1"), increment_kw=Decimal("1"), block=False)

NOW = datetime(2026, 9, 25, 10, 0, tzinfo=UTC)  # before the peak window, same day


def test_candidate_carries_a_positive_value_not_none() -> None:
    """Regression: a null/zero value made the selector's objective coefficient
    `value_per_mwh/1000 - degradation_cost_per_kwh` strictly negative, so `x_o` was never selected."""
    candidates = compute_deferral_candidates(now=NOW, rule=DEFERRAL_RULE)
    assert len(candidates) == 1
    assert candidates[0].value_per_mwh == DEFAULT_DEFERRAL_VALUE_USD_PER_MWH
    assert candidates[0].value_per_mwh > 0
    # Must clear the demo contract's degradation cost (0.03 $/kWh = $30/MWh, 0002_seed_demo.sql) with
    # margin, or the objective coefficient would still be non-positive.
    demo_degradation_usd_per_mwh = Decimal("0.03") * Decimal(1000)
    assert candidates[0].value_per_mwh > demo_degradation_usd_per_mwh


def test_caller_can_override_the_value() -> None:
    candidates = compute_deferral_candidates(now=NOW, rule=DEFERRAL_RULE, value_per_mwh=Decimal("200"))
    assert candidates[0].value_per_mwh == Decimal("200")


def test_window_is_the_next_peak_hour_range() -> None:
    candidates = compute_deferral_candidates(now=NOW, rule=DEFERRAL_RULE)
    window_start, window_end = candidates[0].window_start, candidates[0].window_end
    assert window_end - window_start == timedelta(hours=PEAK_END_HOUR_LOCAL - PEAK_START_HOUR_LOCAL)
    assert window_start > NOW


def test_zero_offer_after_rounding_yields_no_candidates() -> None:
    block_rule = ProductRule(min_qty_kw=Decimal("1000"), increment_kw=Decimal("0"), block=False)
    candidates = compute_deferral_candidates(now=NOW, rule=block_rule, offer_kw=Decimal("500"))
    assert candidates == []
