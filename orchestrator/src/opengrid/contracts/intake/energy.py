"""ERCOT_ENERGY arbitrage candidate generation (02a S1-S3 intake task, task item "ERCOT_ENERGY").
Pure math only -- no I/O -- so it is unit-testable with hand-computed prices (BUILD.md S5a).

Arbitrage rule (per the intake task brief): charge now at the live spot price, discharge at a future
interval's forecast P50 price; the interval is worth offering only if

    spread_usd_per_mwh(t) = discharge_price(t) - charge_price / eta_rt - degradation_usd_per_mwh > 0

`degradation_usd_per_mwh` is the contract's `degradation_cost` ($/kWh, 02a S1.2) converted to $/MWh
(x1000) so it is comparable to the $/MWh price series.

The spread is the admission FILTER only. The candidate's `value_per_mwh` is the GROSS discharge price
(issue #43 A6): the selector's objective already charges wear on every discharged kWh (09 D8) and the
recharge at the zone price + M1 through its charge variables (09 C27/D5), so a value net of both was
counted twice and the selector declined arbitrage the spread says pays.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal

from opengrid.core.timeutil import INTERVAL_MINUTES, floor_to_interval

#: MVP-S has no per-contract round-trip efficiency field (only per-hub `eta_c`/`eta_d`,
#: `opengrid.core.models.platform.Hub`, which intake -- a contracts-level, pre-allocation module --
#: does not read). Approximates the fleet-average round trip (`eta_c * eta_d` at the seed hubs'
#: default 0.9487 each) as a named constant rather than a hidden literal; flagged as a known
#: simplification the allocator's own hub-level efficiency later supersedes at dispatch time.
DEFAULT_ETA_RT = 0.90

#: How far ahead intake looks for a discharge window each gate (2 hours of 15-min slots).
ENERGY_HORIZON_INTERVALS = 8

#: Nominal capacity a contract offers into an ERCOT_ENERGY arbitrage window when MVP-S has no stored
#: per-customer contracted kW (no such column on `og.contract`/`og.product_rule` for a CONTINUOUS
#: product) -- a documented placeholder, per BUILD.md S5a "no silent fallbacks: ... visible", not a
#: guess at a real fleet capacity. `opengrid.core.products.round_quantity` still applies the
#: contract's product rule on top of this.
DEFAULT_ENERGY_OFFER_KW = Decimal("500")


@dataclass(frozen=True, slots=True)
class EnergyCandidate:
    window_start: datetime
    window_end: datetime
    value_per_mwh: Decimal
    """Gross: the forecast P50 discharge price (see the module docstring)."""
    spread_usd_per_mwh: Decimal
    """Net of charge cost / eta_rt and degradation: why the interval was offered."""


def compute_energy_candidates(
    *,
    now: datetime,
    charge_price_usd_per_mwh: float,
    discharge_p50_by_interval: dict[datetime, float],
    degradation_usd_per_kwh: Decimal,
    eta_rt: float = DEFAULT_ETA_RT,
    horizon_intervals: int = ENERGY_HORIZON_INTERVALS,
) -> list[EnergyCandidate]:
    """Return one `EnergyCandidate` per future 15-min interval in `[now's next slot, +horizon)` whose
    spread is positive, `discharge_p50_by_interval` keyed by each interval's `window_start` (UTC,
    floored to the 15-min grid -- `opengrid.forecast.ScenarioPoint.interval_start` already is).
    Never mutates its inputs; callers own admission/tracing."""
    degradation_usd_per_mwh = degradation_usd_per_kwh * Decimal(1000)
    horizon_start = floor_to_interval(now, INTERVAL_MINUTES) + timedelta(minutes=INTERVAL_MINUTES)

    candidates: list[EnergyCandidate] = []
    for i in range(horizon_intervals):
        window_start = horizon_start + timedelta(minutes=INTERVAL_MINUTES * i)
        discharge_price = discharge_p50_by_interval.get(window_start)
        if discharge_price is None:
            continue
        gross = Decimal(str(discharge_price))
        spread = (
            gross - (Decimal(str(charge_price_usd_per_mwh)) / Decimal(str(eta_rt))) - degradation_usd_per_mwh
        )
        if spread > 0:
            candidates.append(
                EnergyCandidate(
                    window_start=window_start,
                    window_end=window_start + timedelta(minutes=INTERVAL_MINUTES),
                    value_per_mwh=gross,
                    spread_usd_per_mwh=spread,
                )
            )
    return candidates
