"""M1 delivery charge (`09-optimizer-dispatcher-update.md` D5): the TDSP's flat per-kWh delivery
charge on kWh drawn from the grid to charge, in the ERCOT competitive area only.

- **Never** inside a regulated utility's own territory (Austin Energy, CPS Energy): their delivery
  cost is already inside the contract's charging terms (`c^{u,grid}_t`), not a separate TDSP line.
- **Never** on behind-the-meter solar: it never crosses the meter, so it was never "drawn from the
  grid" in the first place.
- Applies only to grid-drawn *charging* energy, never to discharge (`settle.profitability`'s
  `energy_cost`/`degradation_cost` price/wear the discharge side; this module is the charging side).

Versioned by `effective_from` (`orchestrator/config/tdsp_tariffs.toml`'s own header rule: "Rates
update about every March 1 and September 1. Add a new `[[tariff]]` block per change; never edit old
blocks."). Resolution is per bank load zone, via `[zone_default_tdsp]` -- a zone with no entry there
(a regulated-utility zone, or one not yet mapped) resolves to no TDSP, which `m1_delivery_charge`
prices at zero, never a guess.

Pure (no I/O) except `load_tdsp_tariffs`/`resolve_tdsp_tariffs_path`, which only read a local file
(BUILD.md S5a "pure logic separated from I/O").
"""

from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from pathlib import Path
from typing import Any

_ZERO = Decimal("0")


@dataclass(frozen=True, slots=True)
class TdspTariff:
    """One `[[tariff]]` block from `tdsp_tariffs.toml`."""

    tdsp: str
    effective_from: date
    volumetric_usd_per_kwh: Decimal
    load_zones: tuple[str, ...]


def resolve_tdsp_tariffs_path(config_path: str | os.PathLike[str] | None = None) -> Path:
    """`OG_TDSP_TARIFFS_PATH` override first; else the sibling `tdsp_tariffs.toml` next to `OG_CONFIG`
    (`orchestrator/config/orchestrator.toml` and `tdsp_tariffs.toml` are both directly under
    `orchestrator/config/`, so `OG_CONFIG`'s own directory is exactly right -- the same
    resolve-from-`OG_CONFIG` approach `opengrid.fleet.seed.resolve_sim_fleet_config_path` uses for its
    sibling-package config file, so this doesn't depend on the process's working directory either);
    else a fresh-checkout-relative default."""
    override = os.environ.get("OG_TDSP_TARIFFS_PATH")
    if override:
        return Path(override)
    og_config = config_path or os.environ.get("OG_CONFIG")
    if og_config:
        return Path(og_config).parent / "tdsp_tariffs.toml"
    return Path("orchestrator/config/tdsp_tariffs.toml")


def load_tdsp_tariffs(path: Path) -> tuple[list[TdspTariff], dict[str, str]]:
    """`([[tariff]] blocks, {load_zone: default_tdsp})` from `tdsp_tariffs.toml`. Raises `OSError` if
    the file is missing -- callers decide how to degrade (BUILD.md S5a "no silent fallbacks"); there
    is no reasonable default TDSP schedule to fall back to."""
    with Path(path).open("rb") as fh:
        raw: dict[str, Any] = tomllib.load(fh)
    tariffs = [
        TdspTariff(
            tdsp=str(t["tdsp"]),
            effective_from=date.fromisoformat(str(t["effective_from"])),
            volumetric_usd_per_kwh=Decimal(str(t["volumetric_usd_per_kwh"])),
            load_zones=tuple(str(z) for z in t.get("load_zones", ())),
        )
        for t in raw.get("tariff", [])
    ]
    zone_default_tdsp = {str(zone): str(tdsp) for zone, tdsp in raw.get("zone_default_tdsp", {}).items()}
    return tariffs, zone_default_tdsp


def tdsp_for_zone(zone_default_tdsp: dict[str, str], zone: str | None) -> str | None:
    """The default TDSP for a bank's load zone (`[zone_default_tdsp]`), or `None` when the zone has
    no entry -- a regulated-utility zone (Austin Energy/CPS Energy) or one not yet mapped. Both mean
    "no TDSP", which `m1_delivery_charge` prices at zero rather than guessing at a TDSP."""
    if zone is None:
        return None
    return zone_default_tdsp.get(zone)


def resolve_tariff(tariffs: list[TdspTariff], tdsp: str | None, as_of: date) -> TdspTariff | None:
    """The tariff in effect for `tdsp` as of `as_of`: the highest `effective_from` that is `<=
    as_of` (the file's own versioning rule: superseding blocks are appended, never edited in place).
    `None` when `tdsp` is `None` (no TDSP resolved: regulated territory or an unmapped zone) or no
    tariff for that TDSP has taken effect yet as of `as_of`."""
    if tdsp is None:
        return None
    candidates = [t for t in tariffs if t.tdsp == tdsp and t.effective_from <= as_of]
    if not candidates:
        return None
    return max(candidates, key=lambda t: t.effective_from)


def grid_charged_kwh_for_delivery(
    delivered_kwh: Decimal, *, eta_c: Decimal, eta_d: Decimal, grid_share: Decimal
) -> Decimal:
    """The grid-drawn charging kWh one obligation-interval's delivery stands for (09 D5's M1 base):
    the AC energy that had to be charged to discharge `delivered_kwh` (delivered / (eta_c x eta_d)), times
    the zone's grid share of charging (`core.solar_share.SolarShare.grid_share`, D-28: solar charging never
    pays M1). Owner decision 2026-09-26: the FULL delivery charge applies to that energy (no Wholesale
    Storage Load or ADER exemption).

    Why by delivery and not by time: settle books one obligation-interval at a time, and charging happens
    in OTHER intervals (overnight, midday) where no obligation is delivering -- attributing only the
    charging seen during the delivery interval itself settled M1 at ~$0 (review finding, 2026-09-26)."""
    if delivered_kwh <= 0 or eta_c <= 0 or eta_d <= 0:
        return _ZERO
    return delivered_kwh / (eta_c * eta_d) * grid_share


def m1_delivery_charge(grid_charged_kwh: Decimal, tariff: TdspTariff | None) -> Decimal:
    """09 D5's M1 charge on `grid_charged_kwh` -- kWh actually drawn from the grid to charge (the
    caller excludes behind-the-meter solar and any regulated-territory asset before calling this;
    this function only ever multiplies). Zero when `tariff` is `None` (no TDSP resolved for the
    asset's zone -- regulated territory, per D5, or an unmapped zone, per BUILD.md S5a "no silent
    fallbacks": a genuinely missing mapping never silently charges the wrong rate, it charges
    nothing, flagged by the caller logging the `None`)."""
    if tariff is None:
        return _ZERO
    return grid_charged_kwh * tariff.volumetric_usd_per_kwh
