"""ogsim.scada.instructions -- rule-based auto utility instruction (02b §4.2).

A simple DMS-like rule: if a bank's kVA loading exceeds its rating for
`overload_consecutive_samples` consecutive readings, auto-issue a LIMIT
instruction (`scada_utility_instruction.schema.json`). Scenario-driven
`utility_instruction` anomalies issue LIMIT/BLOCK/ESTOP directly and
bypass this counter.

Bug fix, 2026-09-26 (R3): the auto-issued LIMIT used `expires_at: None` ("never expires") with no
lift path at all, so a single bank_overload demo left the bank capped at 90% of rating permanently.
`OverloadRule` now tracks each auto-issued LIMIT's own lifecycle and lifts it (`lift_instruction`,
`expires_at` set to its own `issued_at` -- already elapsed on arrival, the same mechanism the
anomaly-driven `utility_instruction` lift already uses, see `ogsim.scada.anomalies`'s
`_utility_instruction_payload(lift=True)`) as soon as EITHER the overload clears for
`clear_samples` consecutive readings, or `max_duration_s` elapses since it was issued -- whichever
comes first. Only one auto-issued LIMIT is ever outstanding per bank at a time (`observe` refuses to
fire again while one is already active), so this rule's own issue/lift pair never overlaps itself.
"""

from __future__ import annotations

from dataclasses import dataclass, field

#: 15 minutes: a bank_overload demo's auto LIMIT must never persist past a bounded worst case, even
#: if the overload somehow never clears (e.g. a stuck anomaly).
DEFAULT_MAX_DURATION_S: float = 900.0
DEFAULT_CLEAR_SAMPLES: int = 3


@dataclass
class OverloadRule:
    threshold_samples: int
    clear_samples: int = DEFAULT_CLEAR_SAMPLES
    max_duration_s: float = DEFAULT_MAX_DURATION_S

    _consecutive: dict[str, int] = field(default_factory=dict)
    _clear_consecutive: dict[str, int] = field(default_factory=dict)
    #: bank_id -> the `now` this rule auto-issued a still-outstanding LIMIT for it, unix epoch
    #: seconds. Present only while that LIMIT has not yet been lifted.
    _active_since: dict[str, float] = field(default_factory=dict)
    #: bank_id -> True while `cancel()` has taken this bank's instruction slot away (a scenario
    #: BLOCK/ESTOP/LIMIT took over) and the overload condition has not yet CLEARED since. R7 fix,
    #: 2026-09-26: `observe()` refuses to re-arm here even while the SAME overload persists
    #: uninterrupted -- a held bank needs a fresh rising edge (clear, then overloaded again) before
    #: this rule will issue a new auto-LIMIT, so a cancel() can never be followed by an immediate
    #: re-arm that replaces a still-active scenario BLOCK/ESTOP.
    _held: dict[str, bool] = field(default_factory=dict)

    def observe(self, bank_id: str, kva_load: float, kva_rating: float, now: float) -> bool:
        """Records one overload sample; returns True the instant a NEW LIMIT should be issued (i.e.
        on the sample that reaches `threshold_samples`), then resets the overload streak so it does
        not re-fire every tick while still overloaded. Never fires while an auto-issued LIMIT for
        `bank_id` is already outstanding (call `check_lift` to end that one first), nor while `bank_id`
        is HELD (see `cancel()`/`_held`'s docstring) and the overload condition has not yet cleared."""
        if bank_id in self._active_since:
            return False
        if self._held.get(bank_id, False):
            if kva_load <= kva_rating:
                # Rising-edge reset: the condition cleared at least once since cancel() -- the next
                # overload is a FRESH trigger, so un-hold and count normally from here.
                self._held[bank_id] = False
                self._consecutive[bank_id] = 0
            return False
        if kva_load > kva_rating:
            count = self._consecutive.get(bank_id, 0) + 1
            self._consecutive[bank_id] = count
            if count >= self.threshold_samples:
                self._consecutive[bank_id] = 0
                self._active_since[bank_id] = now
                self._clear_consecutive[bank_id] = 0
                return True
            return False
        self._consecutive[bank_id] = 0
        return False

    def check_lift(self, bank_id: str, kva_load: float, kva_rating: float, now: float) -> bool:
        """True the instant the auto-issued LIMIT for `bank_id` should be lifted: EITHER
        `clear_samples` consecutive readings back at/under rating, or `max_duration_s` elapsed since
        it was issued -- whichever comes first. Always `False` when no auto-issued LIMIT is
        outstanding for this bank (nothing to lift)."""
        since = self._active_since.get(bank_id)
        if since is None:
            return False
        if now - since >= self.max_duration_s:
            self._reset(bank_id)
            return True
        if kva_load <= kva_rating:
            count = self._clear_consecutive.get(bank_id, 0) + 1
            self._clear_consecutive[bank_id] = count
            if count >= self.clear_samples:
                self._reset(bank_id)
                return True
        else:
            self._clear_consecutive[bank_id] = 0
        return False

    def _reset(self, bank_id: str) -> None:
        self._active_since.pop(bank_id, None)
        self._clear_consecutive.pop(bank_id, None)

    def cancel(self, bank_id: str) -> None:
        """Clears this rule's own bookkeeping for `bank_id` WITHOUT publishing a lift -- called
        whenever a scenario-driven `utility_instruction` (BLOCK/ESTOP/LIMIT) takes over that bank's
        instruction slot (`ScadaEngine.tick`'s `pending` branch). R3.4 fix: without this, a scenario's
        BLOCK/ESTOP could be silently overwritten by this rule's own LATER auto-lift (`check_lift`
        publishes an expired LIMIT the instant the overload clears, and the orchestrator's fleet twin
        stores the LATEST instruction per bank regardless of kind) -- the auto-lift must only ever end
        an auto-LIMIT it itself issued, never a scenario's separate instruction.

        R7 fix, 2026-09-26: this used to also reset the overload-streak counter to 0, which let
        `observe()` re-arm and issue a FRESH auto-LIMIT after just `threshold_samples` more ticks --
        immediately replacing a scenario BLOCK/ESTOP that was still active (the same "latest wins"
        clobbering, one level up). This now HOLDS the bank instead (`_held`): `observe()` won't fire
        again while the overload condition persists uninterrupted, only once it first clears and then
        recurs -- a genuine fresh trigger, not a continuation of the condition the scenario was
        already handling."""
        self._reset(bank_id)
        self._consecutive.pop(bank_id, None)
        self._held[bank_id] = True


def limit_instruction(
    instruction_id: str, bank_id: str, limit_kw: float, issued_at: str
) -> dict[str, object]:
    return {
        "instruction_id": instruction_id,
        "bank_id": bank_id,
        "kind": "LIMIT",
        "limit_kw": limit_kw,
        "issued_at": issued_at,
        "expires_at": None,
        "issued_by": "SCADA_AUTO_RULE",
    }


def lift_instruction(instruction_id: str, bank_id: str, limit_kw: float, issued_at: str) -> dict[str, object]:
    """Lifts a previously auto-issued LIMIT (bug fix, 2026-09-26, R3): the wire schema
    (`scada_utility_instruction.schema.json`) has no separate "lift" kind, so this republishes the
    SAME `kind: LIMIT` (with the same `limit_kw` -- required and non-null whenever kind is LIMIT) but
    sets `expires_at` to its own `issued_at`, already elapsed the instant it arrives -- the identical
    mechanism `ogsim.scada.anomalies`'s `utility_instruction` lift and
    `opengrid.fleet._active_utility_limit_kw` already rely on to end an instruction."""
    return {
        "instruction_id": instruction_id,
        "bank_id": bank_id,
        "kind": "LIMIT",
        "limit_kw": limit_kw,
        "issued_at": issued_at,
        "expires_at": issued_at,
        "issued_by": "SCADA_AUTO_RULE",
    }
