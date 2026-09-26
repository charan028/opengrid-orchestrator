"""ES06-S04 / K7 escalation: TIMEOUT != VETO != STOP, and repeated vetoes degrade, never trip.

Per guardian tick, for every scope (bank, and the zone containing it):

- more than `conservative_ratio` (5 %) of the tick's commands vetoed (a BAD tick) -> the scope goes
  CONSERVATIVE (`og.scope_posture`, which the engine honours with reduced or zero new dispatch there),
  ALR-SCOPE-CONSERVATIVE is raised, and the scope's escalation count goes up by one;
- an escalation count of `stop_request_after` (3) -> a scoped safe stop is REQUESTED of a person:
  ALR-SAFE-STOP-REQUESTED plus an unconfirmed operator-action proposal. Nothing here ever engages a stop;
  an operator uses the normal two-step safe-stop flow (K8);
- hysteresis (review fix: a veto ratio flapping 6 %, 4 %, 6 % used to alternate ENTER/CLEAR and never reach a
  stop request). Only a GOOD tick -- at or under `conservative_ratio x clear_ratio_factor` (2.5 %) -- counts
  toward recovery, and a CONSERVATIVE scope clears back to NORMAL (both alerts cleared) only after
  `clear_after_good_ticks` (3) consecutive good ticks. A tick between the two thresholds is neither: it
  breaks the good streak and leaves the count alone. While CONSERVATIVE, good ticks never lower the count
  (a flapping fault cannot bleed off its own escalation); the count DECAYS by one on the clearing tick and on
  every good tick after it, rather than resetting, so a fault that relapses soon after clearing resumes
  close to a stop request. A CONSERVATIVE scope that sends no commands for `idle_clear_ticks` ticks also
  clears (no evidence left either way; this keeps a scope the engine has throttled to zero from staying
  degraded forever), with the same one-step decay.

Only explicit invariant vetoes count. TIMEOUTs (holds, K7) and batches refused because a safe stop is
already engaged are not vetoes of the batch's content and never count.

The counter lives here, in og-guardian: it is the only process that sees every verdict, so no other module
has to re-derive veto statistics. This module is pure; `main.py` feeds it and applies its transitions
through `ports.ScopePosturePort`.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass, field
from typing import Literal

ScopeKind = Literal["BANK", "ZONE"]
ScopeKey = tuple[ScopeKind, str]
Posture = Literal["NORMAL", "CONSERVATIVE"]
TransitionKind = Literal["ENTER_CONSERVATIVE", "STAY_CONSERVATIVE", "REQUEST_SAFE_STOP", "CLEAR"]

DEFAULT_CONSERVATIVE_RATIO = 0.05
DEFAULT_STOP_REQUEST_AFTER = 3
DEFAULT_IDLE_CLEAR_TICKS = 30
DEFAULT_CLEAR_AFTER_GOOD_TICKS = 3
DEFAULT_CLEAR_RATIO_FACTOR = 0.5

#: Verdict rule ids that are not a veto of the batch's content (a hold or an already-engaged stop).
NON_VETO_RULES = frozenset({"SAFE_STOP"})


@dataclass(frozen=True, slots=True)
class BatchOutcome:
    """One evaluated batch, as the escalation counter sees it."""

    bank_id: str
    commands: int
    vetoed_commands: int  # commands refused by an explicit invariant veto (0 for PASS / TIMEOUT / stop)


def vetoed_command_count(
    outcome: str, rule_ids: Iterable[str], item_hubs: list[str], violated_hubs: Iterable[str | None]
) -> int:
    """How many of a batch's commands an explicit invariant veto refused. VETOED refuses them all;
    PARTLY_VETOED refuses the items on the hubs its item-level violations name; PASS, TIMEOUT and a veto
    whose only reason is an already-engaged safe stop refuse none (K7: TIMEOUT != VETO != STOP)."""
    rules = set(rule_ids)
    if outcome in ("PASS", "TIMEOUT") or not rules or rules <= NON_VETO_RULES:
        return 0
    if outcome == "PARTLY_VETOED":
        hit = {hub for hub in violated_hubs if hub is not None}
        return sum(1 for hub in item_hubs if hub in hit)
    return len(item_hubs)


@dataclass(frozen=True, slots=True)
class Transition:
    scope: ScopeKey
    kind: TransitionKind
    veto_ratio: float
    consecutive: int  # the scope's escalation count (bad ticks not yet decayed), 0 on CLEAR


@dataclass
class _ScopeState:
    posture: Posture = "NORMAL"
    consecutive: int = 0  # escalation count: +1 per bad tick, decays by 1 per good tick once NORMAL
    good_streak: int = 0  # consecutive good ticks while CONSERVATIVE
    idle_ticks: int = 0
    stop_requested: bool = False


@dataclass
class EscalationTracker:
    conservative_ratio: float = DEFAULT_CONSERVATIVE_RATIO
    stop_request_after: int = DEFAULT_STOP_REQUEST_AFTER
    idle_clear_ticks: int = DEFAULT_IDLE_CLEAR_TICKS
    clear_after_good_ticks: int = DEFAULT_CLEAR_AFTER_GOOD_TICKS
    clear_ratio_factor: float = DEFAULT_CLEAR_RATIO_FACTOR
    _states: dict[ScopeKey, _ScopeState] = field(default_factory=dict)

    def posture(self, scope: ScopeKey) -> Posture:
        return self._states.get(scope, _ScopeState()).posture

    def stop_requested(self, scope: ScopeKey) -> bool:
        return self._states.get(scope, _ScopeState()).stop_requested

    def escalation_count(self, scope: ScopeKey) -> int:
        return self._states.get(scope, _ScopeState()).consecutive

    def observe_tick(self, outcomes: list[BatchOutcome], zone_by_bank: dict[str, str]) -> list[Transition]:
        """Fold one tick's batch outcomes into per-scope tallies and return the posture transitions."""
        tallies: dict[ScopeKey, list[int]] = {}
        for outcome in outcomes:
            scopes: list[ScopeKey] = [("BANK", outcome.bank_id)]
            zone = zone_by_bank.get(outcome.bank_id)
            if zone:
                scopes.append(("ZONE", zone))
            for scope in scopes:
                tally = tallies.setdefault(scope, [0, 0])
                tally[0] += outcome.commands
                tally[1] += outcome.vetoed_commands

        transitions: list[Transition] = []
        for scope in sorted(set(tallies) | set(self._states)):
            commands, vetoed = tallies.get(scope, [0, 0])
            transition = self._step(scope, commands, vetoed)
            if transition is not None:
                transitions.append(transition)
        return transitions

    def _step(self, scope: ScopeKey, commands: int, vetoed: int) -> Transition | None:
        state = self._states.setdefault(scope, _ScopeState())
        if commands <= 0:
            if state.posture == "CONSERVATIVE":
                state.idle_ticks += 1
                if state.idle_ticks >= self.idle_clear_ticks:
                    return self._clear(scope, state, 0.0)
            return None
        state.idle_ticks = 0
        ratio = vetoed / commands
        if ratio > self.conservative_ratio:  # bad tick
            entering = state.posture == "NORMAL"
            state.posture = "CONSERVATIVE"
            state.good_streak = 0
            state.consecutive += 1
            if state.consecutive >= self.stop_request_after and not state.stop_requested:
                state.stop_requested = True
                return Transition(scope, "REQUEST_SAFE_STOP", ratio, state.consecutive)
            kind: TransitionKind = "ENTER_CONSERVATIVE" if entering else "STAY_CONSERVATIVE"
            return Transition(scope, kind, ratio, state.consecutive)
        good = ratio <= self.conservative_ratio * self.clear_ratio_factor
        if state.posture == "NORMAL":
            if good:
                state.consecutive = max(state.consecutive - 1, 0)
            return None
        if not good:  # between the thresholds while CONSERVATIVE: not recovered, not worse
            state.good_streak = 0
            return None
        state.good_streak += 1
        if state.good_streak >= self.clear_after_good_ticks:
            return self._clear(scope, state, ratio)
        return None

    def _clear(self, scope: ScopeKey, state: _ScopeState, ratio: float) -> Transition:
        """Back to NORMAL with both alerts cleared; the escalation count decays by one, it does not reset."""
        state.posture = "NORMAL"
        state.stop_requested = False
        state.good_streak = 0
        state.idle_ticks = 0
        state.consecutive = max(state.consecutive - 1, 0)
        return Transition(scope, "CLEAR", ratio, 0)


CONSERVATIVE_ALERT_RULE = "ALR-SCOPE-CONSERVATIVE"
STOP_REQUEST_ALERT_RULE = "ALR-SAFE-STOP-REQUESTED"
