"""ogsim.scada.anomalies -- SCADA_* anomaly catalogue application/reversion.

Driven by `<root>/scenario/cmd` (parsed tolerantly by `ogsim.common.scenario`,
see that module's docstring for the wire-shape compromise). Anomaly effects
are per-bank modifier state applied when building each `scada_bank_signal`
reading; `tick()` reverts anomalies whose duration has elapsed.
"""

from __future__ import annotations

import logging
import math
import re
from dataclasses import dataclass
from typing import Any

from ogsim.scada.aggregation import kva_to_kw

logger = logging.getLogger(__name__)


#: Live bug fix, 2026-09-26 (FLEET-SIM, e2e A11 regression: "a bank_overload injection from
#: ogsim.control never changes the SCADA sim's readings"). Root cause: several shipped scenario files
#: (e.g. `scenarios/bank_overload_and_utility_limit.yaml`'s `target: BANK_07`,
#: `scenarios/compound_stress.yaml`'s `target: BANK_12`) name banks in an uppercase/underscore
#: placeholder format that never matches this sim's real `bank-NNN` ids -- `_resolve_targets` used an
#: exact-match lookup, so the anomaly silently resolved to zero banks and `_apply` had nothing to loop
#: over. This pattern accepts "BANK_07", "bank_07", "bank-007", "BANK07" etc. as all naming the same
#: real bank -- case-insensitive, dash/underscore/no-separator, any digit width.
_BANK_REF_PATTERN = re.compile(r"^bank[-_]?(\d+)$", re.IGNORECASE)


def normalize_bank_ref(ref: str, bank_ids: list[str]) -> str | None:
    """The real bank id `ref` names, or `None` if it names no bank in `bank_ids`. An exact match wins
    first (so a ref that already IS a real id, any shape, always resolves); otherwise `ref` is parsed
    as `bank[-_]?<digits>` (case-insensitive) and re-formatted to this sim's own `bank-{n:03d}` scheme
    for the lookup."""
    if ref in bank_ids:
        return ref
    match = _BANK_REF_PATTERN.match(ref.strip())
    if match is None:
        return None
    candidate = f"bank-{int(match.group(1)):03d}"
    return candidate if candidate in bank_ids else None


SCADA_ANOMALY_TYPES = frozenset(
    {
        "bank_overload",
        "load_spike",
        "frozen_value",
        "bad_quality_flag",
        "stale_no_update",
        "out_of_range_value",
        "oscillation",
        "phase_imbalance",
        "breaker_open",
        "comms_loss",
        "utility_instruction",
        "time_skew",
        "meter_mismatch",
    }
)


@dataclass
class ActiveScadaAnomaly:
    id: str
    type: str
    bank_ids: list[str]
    params: dict[str, Any]
    start: float
    duration: float | None

    def is_active_at(self, now: float) -> bool:
        if now < self.start:
            return False
        return self.duration is None or now < self.start + self.duration


def _utility_instruction_payload(
    bank_id: str, params: dict[str, Any], *, lift: bool = False
) -> dict[str, Any]:
    """Builds the `pending_instruction` bookkeeping dict `ScadaEngine._instruction_message`
    (runtime.py) turns into a wire `ScadaUtilityInstruction`. Shared by `_apply` (issuing) and
    `_revert` (lifting, `lift=True`) so both construct `kind`/`limit_kw` from `params` the
    same way -- never two independent copies of that mapping."""
    mode = str(params.get("mode", "limit"))
    payload: dict[str, Any] = {
        "bank_id": bank_id,
        "kind": mode.upper(),
        "limit_kw": float(params.get("limit_kw", 0.0)) if mode == "limit" else None,
        "lift": lift,
    }
    if lift and params.get("lifts_instruction_id"):
        # Q10 (r3.4.5): cancelling with an explicit id lifts THAT instruction -- e.g. one this sim issued
        # before a restart (its id is in the orchestrator's stop reason); otherwise the sim names its own.
        payload["lifts_instruction_id"] = str(params["lifts_instruction_id"])
    return payload


@dataclass
class BankModifiers:
    overload_pct: float = 0.0
    overload_active: bool = False
    load_multiplier: float = 1.0
    frozen: bool = False
    frozen_value: float | None = None
    quality_override: str | None = None
    suppressed: bool = False
    out_of_range_value: float | None = None
    oscillation: dict[str, float] | None = None
    breaker_open: bool = False
    comms_loss: bool = False
    time_skew_s: float = 0.0
    #: meter_mismatch (D-38): the meter sees battery_scale x the batteries' net power plus offset_kw.
    meter_battery_scale: float = 1.0
    meter_offset_kw: float = 0.0
    pending_instruction: dict[str, Any] | None = None


class ScadaAnomalyManager:
    """Applies/reverts SCADA_* anomalies against per-bank `BankModifiers`."""

    def __init__(self, bank_ids: list[str], zones: list[str]) -> None:
        self.bank_ids = bank_ids
        self.zones = zones
        self.modifiers: dict[str, BankModifiers] = {b: BankModifiers() for b in bank_ids}
        self._active: dict[str, ActiveScadaAnomaly] = {}

    def _resolve_targets(self, target_kind: str | None, target_ref: str) -> list[str]:
        if target_ref in ("*", "") or target_kind == "sim":
            return list(self.bank_ids)
        normalized_bank = normalize_bank_ref(target_ref, self.bank_ids)
        if target_kind == "bank" or normalized_bank is not None:
            return [normalized_bank] if normalized_bank is not None else []
        if target_kind == "zone" or target_ref in self.zones:
            return [b for b, z in zip(self.bank_ids, self.zones, strict=True) if z == target_ref]
        return []

    def start(
        self,
        anomaly_id: str,
        anomaly_type: str,
        target_kind: str | None,
        target_ref: str,
        params: dict[str, Any],
        start: float,
        duration_s: float | None,
    ) -> ActiveScadaAnomaly:
        banks = self._resolve_targets(target_kind, target_ref)
        anomaly = ActiveScadaAnomaly(anomaly_id, anomaly_type, banks, params, start, duration_s)
        if duration_s is not None and duration_s <= 0:
            # A manual cancel republishes the SAME command with duration_s=0 (`ogsim.control.
            # injector.Injector.cancel`: "no cancel verb in the wire schema") -- an
            # already-expired window on arrival, not a fresh application. Go straight to
            # `_revert()` (never `_apply()`) so cancelling never re-issues the anomaly's
            # effect, however briefly -- e.g. a `utility_instruction` BLOCK/LIMIT/ESTOP -- and
            # any resulting lift is queued immediately rather than waiting for the next
            # `tick()` expiry sweep. Also drops any stale entry under this id from a PRIOR
            # `start()` call (defensive; `_active` is keyed by id, so a normal apply already
            # overwrites it, but a cancel never adds one).
            self._active.pop(anomaly_id, None)
            self._revert(anomaly)
            return anomaly
        if not banks:
            # Not an error (behaviour unchanged), but an unknown target silently does nothing.
            logger.warning(
                "anomaly %s (%s) target kind=%s ref=%r matched 0 banks; it has no effect",
                anomaly_id,
                anomaly_type,
                target_kind,
                target_ref,
            )
        self._active[anomaly_id] = anomaly
        self._apply(anomaly, start)
        return anomaly

    def tick(self, now: float) -> None:
        expired = [a for a in self._active.values() if not a.is_active_at(now)]
        for anomaly in expired:
            self._revert(anomaly)
            del self._active[anomaly.id]

    def has_active_utility_instruction(self, bank_id: str, now: float) -> bool:
        """True while a scenario-driven `utility_instruction` anomaly (BLOCK/ESTOP/LIMIT) targeting
        `bank_id` is currently active. R7 fix, 2026-09-26: `ScadaEngine.tick` uses this to keep the
        rule-based auto-LIMIT/auto-lift (`OverloadRule`) completely out of the way of a scenario's own
        instruction for the bank's ENTIRE duration -- not just the tick it was issued on -- so "an
        active scenario BLOCK/ESTOP is never replaced by auto-LIMIT" holds absolutely, regardless of
        whether the overload condition clears and recurs while the scenario instruction is still in
        force."""
        return any(
            a.type == "utility_instruction" and bank_id in a.bank_ids and a.is_active_at(now)
            for a in self._active.values()
        )

    def _apply(self, anomaly: ActiveScadaAnomaly, now: float) -> None:
        for bank_id in anomaly.bank_ids:
            m = self.modifiers[bank_id]
            p = anomaly.params
            if anomaly.type == "bank_overload":
                m.overload_pct = float(p.get("kva_over_rating_pct", 20.0))
                m.overload_active = True
            elif anomaly.type == "load_spike":
                m.load_multiplier = float(p.get("multiplier", 2.0))
            elif anomaly.type == "frozen_value":
                m.frozen = True
            elif anomaly.type == "bad_quality_flag":
                m.quality_override = "out_of_range"
            elif anomaly.type == "stale_no_update":
                m.suppressed = True
                m.quality_override = "stale"
            elif anomaly.type == "out_of_range_value":
                m.out_of_range_value = float(p.get("value", -1.0))
            elif anomaly.type == "oscillation":
                m.oscillation = {
                    "amplitude_pct": float(p.get("amplitude_pct", 15.0)),
                    "period_s": float(p.get("period_s", 10.0)),
                    "start": now,
                }
            elif anomaly.type == "phase_imbalance":
                m.load_multiplier = 1.0 + float(p.get("imbalance_pct", 25.0)) / 100.0
            elif anomaly.type == "breaker_open":
                m.breaker_open = True
                m.quality_override = "comm_fail"
            elif anomaly.type == "comms_loss":
                m.comms_loss = True
                m.suppressed = True
                m.quality_override = "comm_fail"
            elif anomaly.type == "time_skew":
                m.time_skew_s = float(p.get("skew_s", 300.0))
            elif anomaly.type == "meter_mismatch":
                m.meter_battery_scale = float(p.get("battery_scale", 0.5))
                m.meter_offset_kw = float(p.get("offset_kw", 0.0))
            elif anomaly.type == "utility_instruction":
                m.pending_instruction = _utility_instruction_payload(bank_id, p)

    def _revert(self, anomaly: ActiveScadaAnomaly) -> None:
        for bank_id in anomaly.bank_ids:
            m = self.modifiers[bank_id]
            if anomaly.type == "bank_overload":
                m.overload_pct = 0.0
                m.overload_active = False
            elif anomaly.type in ("load_spike", "phase_imbalance"):
                m.load_multiplier = 1.0
            elif anomaly.type == "frozen_value":
                m.frozen = False
                m.frozen_value = None
            elif anomaly.type == "bad_quality_flag":
                m.quality_override = None
            elif anomaly.type == "stale_no_update":
                m.suppressed = False
                m.quality_override = None
            elif anomaly.type == "out_of_range_value":
                m.out_of_range_value = None
            elif anomaly.type == "oscillation":
                m.oscillation = None
            elif anomaly.type == "breaker_open":
                m.breaker_open = False
                m.quality_override = None
            elif anomaly.type == "comms_loss":
                m.comms_loss = False
                m.suppressed = False
                m.quality_override = None
            elif anomaly.type == "time_skew":
                m.time_skew_s = 0.0
            elif anomaly.type == "meter_mismatch":
                m.meter_battery_scale = 1.0
                m.meter_offset_kw = 0.0
            elif anomaly.type == "utility_instruction":
                # Blocker fix: ending a utility_instruction anomaly (natural duration elapse,
                # via tick()'s expiry sweep, OR a manual cancel, via start()'s immediate-revert
                # path above) must LIFT the BLOCK/LIMIT/ESTOP, not just stop re-issuing it --
                # og.hub's fleet twin (opengrid.fleet.ingest_utility_instruction) stores the
                # LATEST instruction per bank forever until a newer one supersedes it, so
                # without this the instruction the anomaly issued when it STARTED stays live
                # in the orchestrator indefinitely. `lift=True` tells `_instruction_message`
                # (runtime.py) to set `expires_at` to the current tick's timestamp -- already
                # in the past on arrival -- which is the SAME "ends via expires_at" mechanism
                # `opengrid.fleet._active_utility_limit_kw` already uses for the rule-based
                # auto-instruction (`instructions.limit_instruction`'s `expires_at: None` is
                # the "never expires" case; this is the opposite, "already expired" case).
                m.pending_instruction = _utility_instruction_payload(bank_id, anomaly.params, lift=True)

    def apply_reading(
        self, bank_id: str, real_power_kw: float, quality: str, now: float, *, kva_rating: float
    ) -> tuple[float, str]:
        """Applies this bank's active modifiers to a computed reading,
        returning `(possibly overridden value, possibly overridden quality)`.
        Overrides operate on the real-power kW value before kVA conversion;
        callers compute kVA from the returned kW.

        Bug fix, 2026-09-26 (#43 B1): a `bank_overload` sets the reading to `kva_rating x (1 +
        kva_over_rating_pct / 100)` kVA while active -- what the param's name, the catalogue entry and
        `scenarios/demo-02-bank-overload.yaml` ("25% over rating, i.e. 750 kVA") all say. It used to
        multiply the actual reading (~200 kW background load), so +25% landed near 250 kVA, far under
        the 600 kVA rating, and no ALR-SCADA-OVERLOAD ever opened."""
        m = self.modifiers[bank_id]
        value = real_power_kw
        if m.out_of_range_value is not None:
            return m.out_of_range_value, "out_of_range"
        if m.frozen:
            if m.frozen_value is None:
                m.frozen_value = value
            return m.frozen_value, quality
        value *= m.load_multiplier
        if m.overload_active:
            value = kva_to_kw(kva_rating * (1.0 + m.overload_pct / 100.0))
        if m.oscillation:
            amp = m.oscillation["amplitude_pct"] / 100.0
            period = max(m.oscillation["period_s"], 1e-6)
            phase = 2.0 * math.pi * (now - m.oscillation["start"]) / period
            value *= 1.0 + amp * math.sin(phase)
        quality_out = m.quality_override or quality
        return value, quality_out

    def take_pending_instruction(self, bank_id: str) -> dict[str, Any] | None:
        m = self.modifiers[bank_id]
        instruction = m.pending_instruction
        m.pending_instruction = None
        return instruction
