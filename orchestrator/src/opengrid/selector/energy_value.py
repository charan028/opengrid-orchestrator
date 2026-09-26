"""Stored-energy value published by each plan for the real-time dispatcher (09 D7, finding G9).

The selector's C1 dual is the water value nu_{b,t}: what one more stored (DC) kWh in interval t is worth
to the plan. Discharging one AC kWh of free headroom at t spends 1/eta_d stored kWh and wears the bank,
so the break-even real-time price is

    discharge_threshold_usd_per_mwh = 1000 * (nu_{b,t} / eta_d + wear_b)

RT discharges headroom only when the live zone price is at or above it (plus its own hysteresis). This
replaces the fixed $30/MWh (`allocator/cycle.py`) with a value that already includes the recharge cost,
M1 and the evening ramp as the scenarios price them.

Each plan also publishes the HARD hold floor e^hold per bank and interval (`hold_floor_kwh`).

Read API for DISPATCH (None when no plan covers `at` for the bank, or -- for the threshold -- when the
plan has no duals, e.g. RULE_FALLBACK: keep the existing fallback threshold then):
- `hold_floor_kwh(bank_id, at)` / `db.load_hold_floors_kwh(bank_ids, at)`: the hard SoC floor.
- `discharge_threshold_usd_per_mwh(bank_id, at)`: in-process, from the latest plan this process solved
  (og-engine runs the selector gates itself); no I/O, safe to call every cycle.
- `opengrid.selector.db.load_energy_value_thresholds(bank_ids, at)`: the same figure from
  `og.plan_energy_value` (after a restart, or from another process).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta

from opengrid.core.timeutil import to_utc
from opengrid.selector.model import AS_HOLD_FLOOR_FRACTION
from opengrid.selector.types import ExtractedPlan, ModelInputs
from opengrid.selector.value import wear_usd_per_kwh


@dataclass(frozen=True, slots=True)
class BankEnergyValue:
    """One bank's stored-energy value over one plan's horizon; index i = interval i of the horizon."""

    bank_id: str
    horizon_start: datetime
    interval_minutes: int
    water_value_usd_per_mwh: tuple[float | None, ...]
    """nu_{b,t} in $/MWh of stored (DC) energy."""
    discharge_threshold_usd_per_mwh: tuple[float | None, ...]
    """Break-even RT price for discharging one AC MWh of headroom: 1000 * (nu / eta_d + wear)."""
    planned_floor_kwh: tuple[float | None, ...]
    """09 S1.8 e^plan: the lowest SoC any scenario plans at the end of the interval (kWh)."""
    hold_floor_kwh: tuple[float, ...] = ()
    """09 S1.8 e^hold, the HARD floor (kWh): reserve plus every held award's full-deployment energy
    (AS H_k, regulated need-basis sustain) and the G-01-ENERGY margin, over the whole interval. Headroom
    discharge must never take the bank below it, whatever the price."""
    solar_share: tuple[float | None, ...] = ()
    """D-28 solar share of charging the plan assumed per interval (None: no share applied)."""
    solar_share_source: tuple[str | None, ...] = ()
    """Its source per interval: TELEMETRY, ERCOT_SOLAR or ASSUMPTION (D-28 "recorded per interval")."""

    @property
    def horizon_end(self) -> datetime:
        return self.horizon_start + timedelta(
            minutes=self.interval_minutes * len(self.water_value_usd_per_mwh)
        )

    def index_at(self, at: datetime) -> int | None:
        offset = (to_utc(at) - to_utc(self.horizon_start)).total_seconds()
        if offset < 0:
            return None
        index = int(offset // (self.interval_minutes * 60))
        return index if index < len(self.water_value_usd_per_mwh) else None

    def threshold_at(self, at: datetime) -> float | None:
        index = self.index_at(at)
        return None if index is None else self.discharge_threshold_usd_per_mwh[index]

    def hold_floor_at(self, at: datetime) -> float | None:
        index = self.index_at(at)
        return None if index is None or index >= len(self.hold_floor_kwh) else self.hold_floor_kwh[index]


def hold_floor_by_bank_interval(inputs: ModelInputs, plan: ExtractedPlan) -> dict[tuple[str, int], float]:
    """e^hold at the START of each interval, exactly the model's hold row: reserve + the G-01-ENERGY
    margin (with a committed hold present) + sum(hold_h / eta_d x reserved kW) over held awards."""
    held = inputs.energy_hold_hours()
    committed_ids = {co.obligation_id for co in inputs.committed}
    bank_by_id = {b.bank_id: b for b in inputs.banks}
    required: dict[tuple[str, int], float] = {}
    committed_hold: set[tuple[str, int]] = set()
    for (oid, bank_id, t), kw in plan.bank_interval_allocation.items():
        bank = bank_by_id.get(bank_id)
        if oid not in held or bank is None or kw <= 0.0:
            continue
        required[bank_id, t] = required.get((bank_id, t), 0.0) + held[oid] / bank.eta_d * kw
        if oid in committed_ids:
            committed_hold.add((bank_id, t))
    floors: dict[tuple[str, int], float] = {}
    for bank in inputs.banks:
        for t in bank.max_discharge_kw:
            margin = (
                AS_HOLD_FLOOR_FRACTION * bank.capacity_kwh if (bank.bank_id, t) in committed_hold else 0.0
            )
            floors[bank.bank_id, t] = bank.reserve_kwh + margin + required.get((bank.bank_id, t), 0.0)
    return floors


def build_energy_values(
    inputs: ModelInputs, plan: ExtractedPlan, horizon_start: datetime
) -> dict[str, BankEnergyValue]:
    """Per-bank series from a plan. The hard hold floor is published for every plan (a rule plan too:
    its holds are as real); the water value and threshold only where the LP produced duals -- a made-up
    value would be worse than none, so they are None otherwise and DISPATCH keeps its fallback."""
    minutes = int(inputs.interval_minutes)
    n = len(inputs.intervals)
    hold = hold_floor_by_bank_interval(inputs, plan)
    lowest_end: dict[tuple[str, int], float] = {}  # (bank, t) -> min over scenarios of SoC at the end of t
    for (b, start_t, _w), soc in plan.soc_by_bank_interval_scenario.items():
        key = (b, start_t - 1)
        lowest_end[key] = min(soc, lowest_end.get(key, soc))
    out: dict[str, BankEnergyValue] = {}
    for bank in inputs.banks:
        if not bank.models_soc:
            continue
        water: list[float | None] = []
        threshold: list[float | None] = []
        floor: list[float | None] = []
        hold_floor: list[float] = []
        wear = wear_usd_per_kwh(bank)
        for t in range(n):
            nu = plan.stored_energy_value.get((bank.bank_id, t))
            water.append(None if nu is None else 1000.0 * nu)
            threshold.append(None if nu is None or bank.eta_d <= 0 else 1000.0 * (nu / bank.eta_d + wear))
            floor.append(lowest_end.get((bank.bank_id, t)))
            # Both ends of t (09 C3'): a hold starting at t+1 already binds the energy spent during t.
            start = hold.get((bank.bank_id, t), bank.reserve_kwh)
            hold_floor.append(max(start, hold.get((bank.bank_id, t + 1), start)))
        out[bank.bank_id] = BankEnergyValue(
            bank_id=bank.bank_id,
            horizon_start=horizon_start,
            interval_minutes=minutes,
            water_value_usd_per_mwh=tuple(water),
            discharge_threshold_usd_per_mwh=tuple(threshold),
            planned_floor_kwh=tuple(floor),
            hold_floor_kwh=tuple(hold_floor),
            solar_share=tuple(bank.solar_share.get(t) for t in range(n)),
            solar_share_source=tuple(bank.solar_share_source.get(t) for t in range(n)),
        )
    return out


_latest: dict[str, BankEnergyValue] = {}


def publish(values: dict[str, BankEnergyValue]) -> None:
    """Make a new plan's values the latest for their banks (called by `gate.run_gate`)."""
    _latest.update(values)


def clear() -> None:
    """Forget every published value (tests)."""
    _latest.clear()


def latest(bank_id: str) -> BankEnergyValue | None:
    return _latest.get(bank_id)


def discharge_threshold_usd_per_mwh(bank_id: str, at: datetime) -> float | None:
    """DISPATCH read API (in-process): the bank's headroom-discharge break-even price at `at` from the
    latest LP plan, or None (no plan covering `at`, or no dual for that interval)."""
    value = _latest.get(bank_id)
    return None if value is None else value.threshold_at(at)


def hold_floor_kwh(bank_id: str, at: datetime) -> float | None:
    """DISPATCH/guardian read API (in-process): the latest plan's HARD SoC floor (kWh) for the bank at
    `at`: headroom discharge must stop there. None when no plan covers `at`."""
    value = _latest.get(bank_id)
    return None if value is None else value.hold_floor_at(at)
