"""FREE-market (ERCOT competitive area) charging cost: the zone price plus the TDSP's M1 delivery charge
on grid-drawn kWh (09 D5, decision log D-19). M1 itself is `opengrid.settle.tariffs`' (its single
owner); this module only composes it with the zone price. Pure.
"""

from __future__ import annotations

from decimal import Decimal

from opengrid.market.charging import ChargingCost, blend
from opengrid.settle.tariffs import TdspTariff, m1_delivery_charge

_ZERO = Decimal("0")
_ONE = Decimal("1")


def free_charging_cost(
    zone: str,
    *,
    wholesale_usd_per_kwh: Decimal,
    tdsp_tariff: TdspTariff | None,
    solar_share: Decimal = _ZERO,
    solar_usd_per_kwh: Decimal | None = None,
) -> ChargingCost:
    """Charging cost in the ERCOT competitive area. Behind-the-meter solar costs its forgone export
    credit (default: the wholesale price) and never pays M1. `tdsp_tariff` is `None` for an unmapped
    zone, which `settle.tariffs.m1_delivery_charge` prices at 0 (flagged in `tariff_ref`)."""
    delivery = m1_delivery_charge(_ONE, tdsp_tariff)
    solar = wholesale_usd_per_kwh if solar_usd_per_kwh is None else solar_usd_per_kwh
    return ChargingCost(
        zone=zone,
        market="FREE",
        utility_id=None,
        period="WHOLESALE",
        solar_share=solar_share,
        solar_usd_per_kwh=solar,
        grid_energy_usd_per_kwh=wholesale_usd_per_kwh,
        delivery_usd_per_kwh=delivery,
        blended_usd_per_kwh=blend(solar_share, solar, wholesale_usd_per_kwh + delivery),
        tariff_ref=f"ERCOT-{zone}+M1-{tdsp_tariff.tdsp}" if tdsp_tariff else f"ERCOT-{zone}+M1-NONE",
    )
