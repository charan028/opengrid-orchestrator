"""TS-08-03: performance % against baseline (worked example, hand-computed, 02a S7.2)."""

from __future__ import annotations

from decimal import Decimal

from opengrid.settle.performance import (
    compute_compliance_pct,
    compute_performance,
    is_need_basis_compliant,
    passes_threshold,
)


def test_compliance_pct_hand_computed():
    """delivered = 1.0 kWh, baseline = 1.25 kWh -> compliance = 1.0 / 1.25 = 0.8 exactly."""
    compliance = compute_compliance_pct(Decimal("1.0"), Decimal("1.25"))
    assert compliance == Decimal("0.8")


def test_no_baseline_gives_no_compliance():
    assert compute_compliance_pct(Decimal("1.0"), None) is None
    assert compute_compliance_pct(Decimal("1.0"), Decimal("0")) is None


def test_passes_threshold_hand_computed_fail_case():
    """theta = 0.1 -> pass floor = 0.9; compliance 0.8 < 0.9 -> fails."""
    assert passes_threshold(Decimal("0.8"), Decimal("0.1")) is False


def test_passes_threshold_hand_computed_pass_case():
    """theta = 0.2 -> pass floor = 0.8; compliance 0.8 >= 0.8 -> passes (boundary inclusive)."""
    assert passes_threshold(Decimal("0.8"), Decimal("0.2")) is True


def test_no_compliance_vacuously_passes():
    assert passes_threshold(None, Decimal("0.1")) is True


def test_compute_performance_worked_example():
    """Full worked example (TS-08-03): delivered 1.0 kWh, baseline 1.25 kWh, theta 0.1.
    compliance = 0.8, pass floor = 0.9, 0.8 < 0.9 -> fails."""
    result = compute_performance(Decimal("1.0"), Decimal("1.25"), Decimal("0.1"))
    assert result.compliance_pct == Decimal("0.8")
    assert result.passed_threshold is False


# -- D-18 need-basis compliance (00-invariants.md K13 "commitments are over a period") --------------


def test_need_basis_compliant_when_delivery_matches_measured_need():
    assert is_need_basis_compliant(Decimal("1.0"), Decimal("2.5"), Decimal("1.0")) is True


def test_need_basis_compliant_when_delivery_exceeds_measured_need():
    assert is_need_basis_compliant(Decimal("1.5"), Decimal("2.5"), Decimal("1.0")) is True


def test_need_basis_not_compliant_when_delivery_is_below_measured_need():
    assert is_need_basis_compliant(Decimal("0.5"), Decimal("2.5"), Decimal("1.0")) is False


def test_need_basis_measured_need_is_capped_at_committed():
    """A measured need that exceeds the reserved maximum can never excuse more than the reservation
    itself covers -- delivery is compared against min(measured_need, committed)."""
    assert is_need_basis_compliant(Decimal("2.5"), Decimal("2.5"), Decimal("5.0")) is True
    assert is_need_basis_compliant(Decimal("2.0"), Decimal("2.5"), Decimal("5.0")) is False


def test_need_basis_defaults_to_compliant_when_no_measured_need_available():
    """No site-meter reading for the interval (PIPELINE_AC has no kWh-shaped meter; a DATA_CENTER
    reading might be missing/stale) -- treated as compliant by default, mirroring
    opengrid.invariants.checks.classify_dip's stance for a need-basis obligation."""
    assert is_need_basis_compliant(Decimal("0.0"), Decimal("2.5"), None) is True
