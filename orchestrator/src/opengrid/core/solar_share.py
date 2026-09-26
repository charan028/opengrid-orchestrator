"""Solar share of battery charging, MEASURED (owner decision D-28, 11-decision-log.md). One owner for the
rule, used by the selector (charging cost, regulated blend) and settle (M1 grid-charged kWh, P&L).

Source priority, per interval:

1. ``TELEMETRY`` -- the batteries' own telemetry: a hub's reported split of its charging
   (``charge_pv_kw`` / ``charge_grid_kw``), else its behind-the-meter PV surplus over home load
   (``pv_kw - home_load_kw``, migration 0027) capped at its charging power;
2. ``ERCOT_SOLAR`` -- where no hub reports, ERCOT's published solar production share for the interval;
3. ``ASSUMPTION`` -- only when neither exists, the 30% planning assumption.

Pure: no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass
from decimal import Decimal
from typing import Literal

SolarShareSource = Literal["TELEMETRY", "ERCOT_SOLAR", "ASSUMPTION"]

#: D-22 / 08 S3c planning assumption, the LAST fallback under D-28.
PLANNING_SOLAR_SHARE = Decimal("0.30")

#: D-28 source 2 feeds (`og.feed_obs`, source 'ERCOT'): ERCOT solar production (actual for past hours,
#: forecast for future ones) over ERCOT system load. The one definition for the selector and settle.
ERCOT_SOLAR_PRODUCT = "np4-737-cd"
ERCOT_SOLAR_ACTUAL_SERIES = "solar_actual"
ERCOT_SOLAR_FORECAST_SERIES = "solar_forecast"
ERCOT_SYSTEM_LOAD_PRODUCT = "np6-345-cd"
ERCOT_SYSTEM_LOAD_SERIES = "total"

_ZERO = Decimal("0")
_ONE = Decimal("1")


def solar_part_of_charge_kw(
    charge_kw: Decimal,
    *,
    charge_pv_kw: Decimal | None = None,
    charge_grid_kw: Decimal | None = None,
    pv_kw: Decimal | None = None,
    home_load_kw: Decimal | None = None,
) -> Decimal | None:
    """The solar part (kW) of one hub sample's charging power `charge_kw` (>= 0), or None when the hub
    does not report enough to tell (then the caller falls back to source 2 or 3).

    The hub's own split wins; else the PV surplus over the home's load (a missing home-load reading
    counts as no load). Never more than the charging power, never negative."""
    if charge_kw <= _ZERO:
        return _ZERO
    if charge_pv_kw is not None:
        return min(max(charge_pv_kw, _ZERO), charge_kw)
    if charge_grid_kw is not None:
        return max(charge_kw - max(charge_grid_kw, _ZERO), _ZERO)
    if pv_kw is None:
        return None
    surplus = max(pv_kw - max(home_load_kw if home_load_kw is not None else _ZERO, _ZERO), _ZERO)
    return min(surplus, charge_kw)


@dataclass(frozen=True, slots=True)
class SolarShare:
    """The solar fraction of charging energy in [0, 1] and the D-28 source it came from."""

    share: Decimal
    source: SolarShareSource

    @property
    def grid_share(self) -> Decimal:
        return _ONE - self.share


def solar_share(
    *,
    measured_charge_kw_sum: Decimal,
    measured_solar_kw_sum: Decimal,
    ercot_solar_share: Decimal | None = None,
) -> SolarShare:
    """D-28 source priority. `measured_*` sum the charging and its solar part over the REPORTING hub
    samples of the interval (same samples, so the ratio is cadence-independent); `ercot_solar_share` is
    ERCOT's published solar share for the interval, None when the feed has nothing."""
    if measured_charge_kw_sum > _ZERO:
        return SolarShare(_clamp(measured_solar_kw_sum / measured_charge_kw_sum), "TELEMETRY")
    if ercot_solar_share is not None:
        return SolarShare(_clamp(ercot_solar_share), "ERCOT_SOLAR")
    return SolarShare(PLANNING_SOLAR_SHARE, "ASSUMPTION")


def ercot_solar_share_of_load(solar_mw: Decimal, load_mw: Decimal) -> Decimal | None:
    """D-28 source 2's number: ERCOT's published solar production over ERCOT load for the same hour and
    area, clamped to [0, 1] (night-time solar is posted slightly negative for auxiliary draw). None when
    load is not positive -- there is no share of zero load. `opengrid.feeds.ercot_solar` reads the two
    feed values; this is the one formula."""
    if load_mw <= _ZERO:
        return None
    return _clamp(solar_mw / load_mw)


def _clamp(value: Decimal) -> Decimal:
    return min(max(value, _ZERO), _ONE)
