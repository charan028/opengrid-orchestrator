"""Continuous per-obligation ENERGY-sufficiency check (K1, 02-architecture/03-decision-engine.md S8.11's
breach-risk model, restricted to MVP-S's simpler "is there enough kWh above reserve for the rest of the
window" question). Runs EVERY allocator cycle for every COMMITTED/DELIVERING obligation, independent of
the S1-S7 power-capability path in `opengrid.allocator.cycle` -- capacity (kW) headroom alone does not
guarantee a hub holds enough ENERGY above reserve to sustain a commitment for its whole remaining
window; a hub can pass every kW check this cycle and still run dry before `window_end`.

Pure logic, no I/O (BUILD.md S5a): `opengrid.engine.gateways` (the only engine file this agent owns)
reads live SoC/reservations and calls `evaluate_energy_sufficiency`/`evaluate_with_substitution`, then
acts on an AT_RISK result (trace + alert; commitment-lock rules are untouched -- this never reallocates
committed capacity to a different obligation, K13).
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass

from opengrid.core.physics import hub_available_energy_kwh

_EPS = 1e-9


@dataclass(frozen=True, slots=True)
class HubEnergyState:
    """One eligible hub's live energy state for an obligation's sufficiency check. `soc_kwh=None`
    means the fleet twin has no live/fresh SoC for this hub this cycle -- K7's conservative rule: it
    contributes ZERO kWh, never an assumed/stale value (mirrors `opengrid.allocator.cycle`'s hub-level
    missing-SoC handling)."""

    hub_id: str
    soc_kwh: float | None
    reserve_kwh: float
    eta_d: float


@dataclass(frozen=True, slots=True)
class EnergySufficiencyResult:
    """One obligation's energy-sufficiency verdict for the remainder of its committed window."""

    obligation_id: str
    required_kwh: float
    available_kwh: float
    margin_kwh: float
    time_to_depletion_h: float | None  # None: draw rate is 0, so depletion is not defined/relevant
    at_risk: bool
    used_substitution: bool = False


def hub_available_kwh_net_of_other_reservations(
    hub: HubEnergyState, reserved_kwh_by_hub_for_others: Mapping[str, float]
) -> float:
    """K2 ("energy has one buyer too"): a hub's energy above reserve, net of whatever is already
    reserved for OTHER committed obligations sharing this hub. Never negative."""
    if hub.soc_kwh is None:
        return 0.0
    gross_kwh = hub_available_energy_kwh(hub.soc_kwh, hub.reserve_kwh, hub.eta_d)
    reserved_elsewhere_kwh = reserved_kwh_by_hub_for_others.get(hub.hub_id, 0.0)
    return max(gross_kwh - reserved_elsewhere_kwh, 0.0)


def evaluate_energy_sufficiency(
    obligation_id: str,
    committed_kw: float,
    remaining_window_h: float,
    eligible_hubs: Sequence[HubEnergyState],
    reserved_kwh_by_hub_for_others: Mapping[str, float],
) -> EnergySufficiencyResult:
    """The core check: does this obligation's committed delivery profile over
    `[now, window_end]` (`required_kwh = committed_kw * remaining_window_h`, MVP-S's flat-profile
    simplification of the spec's $\\hat y_{o,b,t}$ integral) fit inside the energy its eligible hubs can
    still deliver above reserve, net of energy already promised to OTHER obligations on those hubs?

    `at_risk` is true if the margin is already negative OR the obligation is projected to deplete its
    hubs' energy before `window_end` at the current committed draw rate -- catching the case where the
    margin is still (barely) positive today but the depletion clock is shorter than the remaining window.
    """
    required_kwh = max(committed_kw, 0.0) * max(remaining_window_h, 0.0)
    available_kwh = sum(
        hub_available_kwh_net_of_other_reservations(hub, reserved_kwh_by_hub_for_others)
        for hub in eligible_hubs
    )
    margin_kwh = available_kwh - required_kwh
    time_to_depletion_h = (available_kwh / committed_kw) if committed_kw > _EPS else None
    at_risk = margin_kwh < -_EPS or (
        time_to_depletion_h is not None and time_to_depletion_h < remaining_window_h - _EPS
    )
    return EnergySufficiencyResult(
        obligation_id=obligation_id,
        required_kwh=required_kwh,
        available_kwh=available_kwh,
        margin_kwh=margin_kwh,
        time_to_depletion_h=time_to_depletion_h,
        at_risk=at_risk,
    )


def evaluate_with_substitution(
    obligation_id: str,
    committed_kw: float,
    remaining_window_h: float,
    primary_hubs: Sequence[HubEnergyState],
    substitute_hubs: Sequence[HubEnergyState],
    reserved_kwh_by_hub_for_others: Mapping[str, float],
) -> EnergySufficiencyResult:
    """K13-safe substitution attempt: if the primary eligible set is AT_RISK, retry with the obligation's
    OTHER eligible hubs' energy added in (S5.3's "substitution of homes within the same obligation is
    always allowed and is not an interrupt") BEFORE declaring the obligation at risk. Never touches any
    other obligation's reservation -- `reserved_kwh_by_hub_for_others` already excludes this obligation's
    own claims by construction (the caller computes it from OTHER obligations' reservations only)."""
    primary_result = evaluate_energy_sufficiency(
        obligation_id, committed_kw, remaining_window_h, primary_hubs, reserved_kwh_by_hub_for_others
    )
    if not primary_result.at_risk or not substitute_hubs:
        return primary_result

    combined_hubs = list(primary_hubs) + [
        h for h in substitute_hubs if h.hub_id not in {p.hub_id for p in primary_hubs}
    ]
    combined_result = evaluate_energy_sufficiency(
        obligation_id, committed_kw, remaining_window_h, combined_hubs, reserved_kwh_by_hub_for_others
    )
    if combined_result.at_risk:
        return combined_result
    return EnergySufficiencyResult(
        obligation_id=combined_result.obligation_id,
        required_kwh=combined_result.required_kwh,
        available_kwh=combined_result.available_kwh,
        margin_kwh=combined_result.margin_kwh,
        time_to_depletion_h=combined_result.time_to_depletion_h,
        at_risk=False,
        used_substitution=True,
    )
