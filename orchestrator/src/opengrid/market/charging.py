"""Charging-cost model (08 S3.5, S3a, S3b; 09 D4, D5, S1.6; decision log D-19, D-22).

Per kWh charged:

    c_in = s * c_sol + (1 - s) * (c_grid + w)

- REGULATED territory (Austin Energy, CPS Energy): `s` is at least the utility's solar share floor
  (0.30, a floor, not a fixed ratio); `c_sol` is the utility solar cost (config, 4.0 cents);
  `c_grid` is the utility's charging tariff for the interval (AE TOU pilot: off-peak 2.677 cents at
  night and weekends) plus contract adders; `w = 0` -- AE and CPS are not TDSPs, their delivery cost is
  inside the utility rate.
- FREE (ERCOT competitive area): `c_grid` is the load-zone wholesale price (the caller passes it);
  solar is behind-the-meter PV surplus valued at its forgone export credit (default: the same
  wholesale price) and never pays M1; `w` is the TDSP's flat M1 delivery charge on grid-drawn kWh,
  resolved through `opengrid.settle.tariffs` (the single owner of M1). The FREE side lives in
  `opengrid.market.free_charging` so this module never imports `opengrid.settle` and settle can
  import it without an import cycle.

Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Literal

from opengrid.core.models.market import Market, Utility, UtilityId
from opengrid.core.timeutil import to_market_tz

TouPeriod = Literal["OFF_PEAK", "MID_PEAK", "ON_PEAK", "NIGHT", "DAY", "WHOLESALE"]

_ZERO = Decimal("0")
_ONE = Decimal("1")

# AE residential TOU pilot periods (research doc S2.2), America/Chicago wall clock.
_OFF_PEAK_START_H = 22
_OFF_PEAK_END_H = 7
_ON_PEAK_START_H = 15
_ON_PEAK_END_H = 18


def tou_period(utility: Utility, interval_start: datetime) -> TouPeriod:
    """The utility's charging-tariff period for the interval starting at `interval_start` (any tz;
    naive is taken as UTC by `core.timeutil`). TOU_OFF_PEAK utilities (AE): weekends are off-peak all
    day; weekdays 22:00-07:00 off-peak, 15:00-18:00 on-peak, otherwise mid-peak. NIGHT_RATE utilities:
    22:00-07:00 NIGHT, otherwise DAY."""
    local = to_market_tz(interval_start)
    night = local.hour >= _OFF_PEAK_START_H or local.hour < _OFF_PEAK_END_H
    if utility.charging_tariff_kind == "NIGHT_RATE":
        return "NIGHT" if night else "DAY"
    if local.weekday() >= 5 or night:
        return "OFF_PEAK"
    if _ON_PEAK_START_H <= local.hour < _ON_PEAK_END_H:
        return "ON_PEAK"
    return "MID_PEAK"


def utility_grid_rate(utility: Utility, period: TouPeriod) -> Decimal:
    """The utility's grid charging energy rate for `period`, excluding adders. A period without its own
    published rate falls back to the next higher known rate, never to a cheaper one (a missing
    on-peak rate is priced at mid-peak or, failing that, off-peak -- flagged via `tariff_ref`)."""
    if period in ("OFF_PEAK", "NIGHT"):
        return utility.off_peak_rate_usd_per_kwh
    if period == "ON_PEAK":
        for rate in (utility.on_peak_rate_usd_per_kwh, utility.mid_peak_rate_usd_per_kwh):
            if rate is not None:
                return rate
        return utility.off_peak_rate_usd_per_kwh
    if utility.mid_peak_rate_usd_per_kwh is not None:
        return utility.mid_peak_rate_usd_per_kwh
    return utility.off_peak_rate_usd_per_kwh


@dataclass(frozen=True, slots=True)
class ChargingCost:
    """The cost of one kWh charged in `zone` during one interval, by component.

    `grid_energy_usd_per_kwh` and `delivery_usd_per_kwh` are per GRID-drawn kWh;
    `solar_usd_per_kwh` is per solar kWh; `blended_usd_per_kwh` is per kWh charged at `solar_share`."""

    zone: str
    market: Market
    utility_id: UtilityId | None
    period: TouPeriod
    solar_share: Decimal
    solar_usd_per_kwh: Decimal
    grid_energy_usd_per_kwh: Decimal
    delivery_usd_per_kwh: Decimal
    blended_usd_per_kwh: Decimal
    tariff_ref: str

    @property
    def grid_all_in_usd_per_kwh(self) -> Decimal:
        return self.grid_energy_usd_per_kwh + self.delivery_usd_per_kwh


def blend(solar_share: Decimal, solar_usd_per_kwh: Decimal, grid_all_in_usd_per_kwh: Decimal) -> Decimal:
    """08 S3b: `c_in = s * c_sol + (1 - s) * c_grid_all_in`."""
    if not _ZERO <= solar_share <= _ONE:
        raise ValueError(f"solar_share must be in [0, 1], got {solar_share}")
    return solar_share * solar_usd_per_kwh + (_ONE - solar_share) * grid_all_in_usd_per_kwh


def regulated_charging_cost(
    utility: Utility, zone: str, interval_start: datetime, *, solar_share: Decimal | None = None
) -> ChargingCost:
    """Charging cost inside `utility`'s territory. `solar_share` defaults to the utility's floor (the
    08 S3c planning mix); the optimizer may charge more solar than the floor, never assume less
    (a share below the floor is still costed, since the floor is soft, 09 D4)."""
    share = utility.solar_share_floor if solar_share is None else solar_share
    period = tou_period(utility, interval_start)
    grid = utility_grid_rate(utility, period) + utility.charging_adder_usd_per_kwh
    return ChargingCost(
        zone=zone,
        market="REGULATED",
        utility_id=utility.utility_id,
        period=period,
        solar_share=share,
        solar_usd_per_kwh=utility.solar_cost_usd_per_kwh,
        grid_energy_usd_per_kwh=grid,
        delivery_usd_per_kwh=_ZERO,
        blended_usd_per_kwh=blend(share, utility.solar_cost_usd_per_kwh, grid),
        tariff_ref=utility.tariff_ref,
    )
