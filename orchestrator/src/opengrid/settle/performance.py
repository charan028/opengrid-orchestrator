"""Performance % against baseline (02a S7.2).

`C_{o,j} = D_{o,j} / K_{o,j}`, passes if `C_{o,j} >= theta_o`'s complement, i.e. the pass threshold
is `1 - contract.penalty_theta` (02a S7.2's tolerance-band notation: `penalty_theta` is the
*tolerance fraction*, so the compliance floor is one minus it).
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.settle.models import PerformanceResult

_FULL = Decimal("1")
#: numeric(14,6) column precision (matches opengrid.settle's own `_KWH_EPSILON`) -- a dip that is only
#: a rounding artifact of the measured need must not fail the need-basis compliance check.
_KWH_TOLERANCE = Decimal("0.000001")


def compute_compliance_pct(delivered_kwh: Decimal, baseline_kwh: Decimal | None) -> Decimal | None:
    """`C_{o,j} = D_{o,j} / K_{o,j}`. `None` when there is no baseline (HOME) or it is zero (a
    zero-committed-kw interval has no meaningful compliance ratio)."""
    if baseline_kwh is None or baseline_kwh == 0:
        return None
    return delivered_kwh / baseline_kwh


def passes_threshold(compliance_pct: Decimal | None, penalty_theta: Decimal | None) -> bool:
    """`compliance_pct >= 1 - penalty_theta`. No baseline or no stated tolerance -> vacuously passes
    (nothing to measure against, e.g. HOME)."""
    if compliance_pct is None:
        return True
    theta = penalty_theta if penalty_theta is not None else Decimal("0")
    return compliance_pct >= _FULL - theta


def is_need_basis_compliant(
    delivered_kwh: Decimal,
    committed_kwh: Decimal,
    measured_need_kwh: Decimal | None,
) -> bool:
    """D-18 / 00-invariants.md K13 "commitments are over a period" (need basis, owner decision
    2026-09-26): for an obligation whose service profile is `MEASURED_FEEDBACK` (DATA_CENTER,
    PIPELINE_AC), the committed kWh is a RESERVED MAXIMUM, not a fixed schedule -- delivery below it
    that follows the customer's measured need is compliant, not a shortfall, and is never penalised
    (`R-GRANT-CLOSED-LOOP`; mirrors `opengrid.invariants.checks.classify_dip`'s `is_need_basis`
    branch, the identical rule already applied to K13 lock violations).

    `measured_need_kwh` is the site-meter-derived need for the interval (migration 0015's `og.
    customer_site_meter_reading`), when available. When it is `None` -- no reading for this customer/
    interval, or (PIPELINE_AC) no kWh-shaped site meter exists at all -- the dip is treated as
    compliant by default: the same "innocent until shown otherwise" stance `classify_dip` takes,
    since there is no independent way yet to tell "the customer didn't need it" from "delivery was
    actually short" (`opengrid.invariants.checks.classify_dip`'s own deferred-scope note)."""
    if measured_need_kwh is None:
        return True
    return delivered_kwh >= min(measured_need_kwh, committed_kwh) - _KWH_TOLERANCE


def compute_performance(
    delivered_kwh: Decimal,
    baseline_kwh: Decimal | None,
    penalty_theta: Decimal | None,
) -> PerformanceResult:
    """02a S7.2's full compliance computation for one obligation-interval."""
    compliance_pct = compute_compliance_pct(delivered_kwh, baseline_kwh)
    return PerformanceResult(
        compliance_pct=compliance_pct,
        passed_threshold=passes_threshold(compliance_pct, penalty_theta),
    )
