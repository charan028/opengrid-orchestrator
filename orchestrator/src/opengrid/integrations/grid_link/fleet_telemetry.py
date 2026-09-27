"""`TelemetryPort` over the in-process fleet twin (`opengrid.fleet`), for the grid link running inside
og-engine (grid-link.md S6.2). Reads only; the twin already holds the latest telemetry.

- available kW: `fleet.capability(bank).max_discharge_kw` (health, SoC headroom and any active L2
  instruction already folded in, K1/K5);
- delivered kW: discharge magnitude of the bank's online hubs (telemetry `p_kw` is +charge/-discharge);
- SoC %: stored energy over capacity of the bank's online hubs; None when none reports SoC.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime

from opengrid import fleet
from opengrid.integrations.grid_link.model import BankStatus

__all__ = ["FleetTelemetryPort", "bank_status_from_hubs", "banks_of_zone"]


def bank_status_from_hubs(
    bank_id: str, available_kw: float, hubs: Sequence[fleet.HubCapabilitySnapshot]
) -> BankStatus:
    """Aggregate one bank's online hubs into a `BankStatus`. Pure."""
    soc_kwh = 0.0
    capacity_kwh = 0.0
    net_kw = 0.0
    for hub in hubs:
        if hub.health != "online":
            continue
        if hub.soc_kwh is not None and hub.e_kwh:
            soc_kwh += hub.soc_kwh
            capacity_kwh += hub.e_kwh
        net_kw += hub.p_kw or 0.0
    return BankStatus(
        bank_id=bank_id,
        soc_pct=round(100.0 * soc_kwh / capacity_kwh, 2) if capacity_kwh > 0 else None,
        available_kw=round(max(0.0, available_kw), 3),
        delivered_kw=round(max(0.0, -net_kw), 3),
        soc_kwh=soc_kwh,
        capacity_kwh=capacity_kwh,
    )


class FleetTelemetryPort:
    """Reads the fleet twin of this process."""

    async def bank_status(self, bank_ids: Sequence[str]) -> list[BankStatus]:
        now = datetime.now(UTC)
        out: list[BankStatus] = []
        for bank_id in bank_ids:
            try:
                capability = await fleet.capability(bank_id, now)
                hubs = fleet.hub_capabilities(bank_id)
            except LookupError:
                continue  # not in this fleet: reported as missing telemetry, never invented
            out.append(bank_status_from_hubs(bank_id, capability.max_discharge_kw, hubs))
        return out


def banks_of_zone(zone: str) -> list[str]:
    """Every bank of the fleet twin in `zone` (zone-scoped L2 targets)."""
    return [bank for bank in fleet.known_bank_ids() if fleet.bank_zone(bank) == zone]
