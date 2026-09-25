"""Performance % against baseline (02a S7.2).

`C_{o,j} = D_{o,j} / K_{o,j}`, passes if `C_{o,j} >= theta_o`'s complement, i.e. the pass threshold
is `1 - contract.penalty_theta` (02a S7.2's tolerance-band notation: `penalty_theta` is the
*tolerance fraction*, so the compliance floor is one minus it).
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.settle.models import PerformanceResult

_FULL = Decimal("1")


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
