"""The measured solar share of charging for the plan (owner decision D-28), per zone and local hour.

The forward-looking plan uses the RECENT measured share: the trailing 7-day same-hour share per load
zone. It is sampled from the live fleet twin (hub telemetry, already in this process) at every gate --
a 15-minute sample of every charging hub -- rather than by scanning a week of 2-second `og.telemetry`
rows on every gate. After a restart the history is empty until it refills, and the plan falls back to
ERCOT's solar share, then the 30% assumption (`opengrid.core.solar_share`, the single owner of the rule
and the priority); the source used is published per bank and interval.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Any

from opengrid.core.solar_share import SolarShare, solar_part_of_charge_kw, solar_share
from opengrid.core.timeutil import to_market_tz

#: The trailing window of the measured share (D-28 "trailing 7-day same-hour share per zone").
HISTORY_DAYS = 7


@dataclass(frozen=True, slots=True)
class _Sample:
    at: datetime
    charge_kw: Decimal
    solar_kw: Decimal


def _dec(value: Any) -> Decimal | None:
    return None if value is None else Decimal(repr(float(value)))


class SolarShareHistory:
    """(zone, local hour) -> the gate samples of the trailing `HISTORY_DAYS`."""

    def __init__(self) -> None:
        self._samples: dict[tuple[str, int], deque[_Sample]] = {}

    def clear(self) -> None:
        self._samples.clear()

    def record(self, zone: str, at: datetime, charge_kw: Decimal, solar_kw: Decimal) -> None:
        if charge_kw <= 0:
            return
        window = self._samples.setdefault((zone, to_market_tz(at).hour), deque())
        window.append(_Sample(at, charge_kw, solar_kw))
        while window and window[0].at < at - timedelta(days=HISTORY_DAYS):
            window.popleft()

    def record_hubs(self, zone: str, at: datetime, hubs: Iterable[Any]) -> None:
        """One gate's sample of a zone's hubs (fleet `HubCapabilitySnapshot`s: `p_kw` > 0 is charging).
        Only reporting hubs count; the rule is `core.solar_share.solar_part_of_charge_kw`."""
        charge = Decimal("0")
        solar = Decimal("0")
        for hub in hubs:
            charge_kw = _dec(getattr(hub, "p_kw", None))
            if charge_kw is None or charge_kw <= 0:
                continue
            part = solar_part_of_charge_kw(
                charge_kw,
                charge_pv_kw=_dec(getattr(hub, "charge_pv_kw", None)),
                charge_grid_kw=_dec(getattr(hub, "charge_grid_kw", None)),
                pv_kw=_dec(getattr(hub, "pv_kw", None)),
                home_load_kw=_dec(getattr(hub, "home_load_kw", None)),
            )
            if part is None:
                continue
            charge += charge_kw
            solar += part
        self.record(zone, at, charge, solar)

    def measured(self, zone: str, hour: int, now: datetime) -> tuple[Decimal, Decimal]:
        """(charging kW sum, solar part sum) of the zone's samples at this local hour, trailing week."""
        charge = Decimal("0")
        solar = Decimal("0")
        for sample in self._samples.get((zone, hour), ()):
            if sample.at >= now - timedelta(days=HISTORY_DAYS):
                charge += sample.charge_kw
                solar += sample.solar_kw
        return charge, solar


#: The gate's history (og-engine runs every gate in one process).
history = SolarShareHistory()


def sample_fleet(
    zone_by_bank: Mapping[str, str | None],
    at: datetime,
    hub_capabilities: Callable[[str], Iterable[Any]],
    store: SolarShareHistory = history,
) -> None:
    """Record this gate's telemetry sample per zone from the fleet twin."""
    hubs_by_zone: dict[str, list[Any]] = {}
    for bank_id, zone in zone_by_bank.items():
        if zone is None:
            continue
        hubs_by_zone.setdefault(zone, []).extend(hub_capabilities(bank_id))
    for zone, hubs in hubs_by_zone.items():
        store.record_hubs(zone, at, hubs)


def planned_shares(
    zone: str | None,
    interval_starts: Iterable[tuple[int, datetime]],
    now: datetime,
    ercot_share_by_hour: Mapping[int, float],
    store: SolarShareHistory = history,
) -> dict[int, SolarShare]:
    """The D-28 share for each planned interval of a bank in `zone`: its zone's measured same-hour share,
    else ERCOT's same-hour solar share, else the planning assumption."""
    out: dict[int, SolarShare] = {}
    for t, start in interval_starts:
        hour = to_market_tz(start).hour
        charge, solar = store.measured(zone, hour, now) if zone else (Decimal("0"), Decimal("0"))
        ercot = ercot_share_by_hour.get(hour)
        out[t] = solar_share(
            measured_charge_kw_sum=charge,
            measured_solar_kw_sum=solar,
            ercot_solar_share=None if ercot is None else Decimal(repr(ercot)),
        )
    return out
