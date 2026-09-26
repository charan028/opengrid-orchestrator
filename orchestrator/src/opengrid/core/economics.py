"""Shared dispatch/settlement economics formulas (BUILD.md S1 "no duplicated functions"; `core` is
the single owner so the selector and settle never diverge on how a cost is computed from a quantity).

Currently holds the wear/degradation-cost rule (Frank #7, `09-optimizer-dispatcher-update.md` D8):
"Wear is charged on AC kWh actually discharged, at the asset-class rate, for every purpose. It is
never charged on capacity held, on reservations, or on charging. One function
(`core.economics.wear_cost`) is used by both the selector and settle."
"""

from __future__ import annotations

from decimal import Decimal

_ZERO = Decimal("0")


def wear_cost(kwh_discharged: Decimal, wear_rate_usd_per_kwh: Decimal) -> Decimal:
    """`09` D8's wear rule: cost = AC kWh actually discharged x the discharging asset's own wear rate
    ($/kWh, e.g. `contract.degradation_cost`'s $0.03/kWh default for a home, or a substation asset's
    $0.015/kWh), charged identically for every purpose -- firm delivery, REG capacity delivery, FREE
    energy, AS deployment, or HOME self-serve.

    Callers resolve `wear_rate_usd_per_kwh` themselves (this function does not know about asset
    classes or contracts) and must pass already-DISCHARGED kWh only:
    - the selector passes its LP's *expected* discharged kWh (`d^F`, `y`, the expected AS deployment
      `psi*r` -- never `r`, `y_bar`, or the unused need-basis reserve, which are held, not discharged);
    - `opengrid.settle` passes `meter_interval.delivered_kwh`, the *metered* discharged kWh.

    Both call this one function so the plan's expected wear and settle's billed wear can never drift
    apart from re-deriving the multiplication independently (BUILD.md S1 dupcheck). Never charged on
    charging: raises rather than silently accepting a negative (charging) quantity.
    """
    if kwh_discharged < _ZERO:
        raise ValueError(
            "wear_cost's kwh_discharged must be >= 0 -- wear is never charged on charging (09 D8); "
            f"got {kwh_discharged}"
        )
    return kwh_discharged * wear_rate_usd_per_kwh
