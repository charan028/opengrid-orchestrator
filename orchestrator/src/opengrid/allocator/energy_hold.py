"""ERCOT_AS energy hold (Frank #6; 09 D6, F7; NPRR1282), matching the guardian's G-01-ENERGY margin.

While an AS award is held, the bank keeps enough stored energy for a FULL deployment of the award's kW
over the product's duration (Non-Spin 4 h, ECRS 1 h), on top of the reserve floor and the guardian's
G-01-ENERGY margin (`core.limits.check_reserve_floor_over_lease`, 1 % of each hub's capacity):

    floor_kwh = reserve + 1% x e_kwh + kW x duration / eta_d        (stored kWh, per bank)

Headroom (uncommitted FREE discharge) may spend only the stored energy ABOVE that floor, so a deployment
arriving mid-lease can always run to completion without the guardian vetoing its last leases on
G-01-ENERGY. The same margin feeds the engine's continuous energy-sufficiency check, so an AS award is
flagged AT_RISK exactly when this floor is not met.

Pure functions; no I/O.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence

from opengrid.allocator.models import HubSnapshot, ObligationCall

#: The guardian's G-01-ENERGY margin: `core.limits.check_reserve_floor_over_lease(margin_pct=0.01)`, a
#: fraction of each hub's usable capacity kept above the reserve at lease end.
HOLD_MARGIN_FRACTION = 0.01
#: Full-deployment duration for an AS award whose product rule gives none: ECRS, the shortest (1 h).
DEFAULT_AS_DEPLOYMENT_H = 1.0

_EPS = 1e-9


def hold_duration_h(call: ObligationCall) -> float:
    """The award's full-deployment duration in hours (its product rule's, else ECRS's 1 h)."""
    if call.hold_duration_h is not None and call.hold_duration_h > 0:
        return call.hold_duration_h
    return DEFAULT_AS_DEPLOYMENT_H


def hold_energy_kwh(hold_kw: float, duration_h: float, eta_d: float) -> float:
    """Stored kWh a full deployment draws: `kW x duration / eta_d` (never negative)."""
    return max(hold_kw, 0.0) * max(duration_h, 0.0) / max(eta_d, _EPS)


def margin_kwh(hubs: Iterable[HubSnapshot]) -> float:
    """G-01-ENERGY's margin over the hubs with a live SoC: `1% x e_kwh` each (stored kWh)."""
    return sum(HOLD_MARGIN_FRACTION * (h.e_kwh or 0.0) for h in hubs if h.soc_kwh is not None)


def stored_above_reserve_kwh(hubs: Iterable[HubSnapshot]) -> float:
    """Stored kWh above each hub's reserve floor, over hubs with a live SoC. A hub without one counts
    0 kWh (K1/K7: a missing SoC never counts as energy)."""
    total = 0.0
    for hub in hubs:
        if hub.soc_kwh is None or hub.reserve_kwh is None or not hub.is_healthy:
            continue
        total += max(hub.soc_kwh - hub.reserve_kwh, 0.0)
    return total


def headroom_energy_cap_kw(
    hubs: Sequence[HubSnapshot], holds: Sequence[ObligationCall], lease_ttl_s: float
) -> float:
    """The most headroom discharge (kW) a bank may take over one lease without eroding its AS holds:
    stored energy above `reserve + 1% x e_kwh + sum(kW x duration / eta_d)` spread over the lease.

    0 when there is no energy above the hold floor (including when no hub reports a live SoC)."""
    live = [h for h in hubs if h.soc_kwh is not None and h.reserve_kwh is not None and h.is_healthy]
    if not live:
        return 0.0
    eta_d = sum(h.eta_d for h in live) / len(live)
    held_kwh = sum(hold_energy_kwh(c.committed_kw, hold_duration_h(c), eta_d) for c in holds)
    excess_kwh = stored_above_reserve_kwh(live) - margin_kwh(live) - held_kwh
    lease_h = lease_ttl_s / 3600.0
    if excess_kwh <= _EPS or lease_h <= 0:
        return 0.0
    return excess_kwh * eta_d / lease_h


def deliverable_margin_kwh(e_kwh_by_hub: Iterable[tuple[float | None, float]]) -> float:
    """The G-01-ENERGY margin in DELIVERABLE kWh (`1% x e_kwh x eta_d` per hub), for the engine's
    energy-sufficiency check, which compares deliverable energy above reserve with what is owed.
    Input: `(e_kwh, eta_d)` per hub with a live SoC."""
    return sum(HOLD_MARGIN_FRACTION * (e or 0.0) * eta_d for e, eta_d in e_kwh_by_hub)
