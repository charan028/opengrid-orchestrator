"""Where a utility-scale hub's next G-04 step starts: the last SIGNED setpoint, never the last proposal.

HIGH-A (r3.4.2 toll ramp, verified 2026-09-27): the engine stepped a utility-scale hub from its last
PROPOSED setpoint, refreshing that anchor on every proposal -- vetoed, stopped or unsigned alike -- so it
never re-anchored. After a signing gap of 30 s or more (a safe stop and release, a guardian restart) the
engine walked its own unsigned proposals to -20 MW while the guardian (`guardian.checks.g04_anchor_kw`)
anchored at telemetry 0, and G-04 vetoed every cycle for the rest of the call (80/80).

The contract with the guardian's G-04 (agreed with SAFETY):

- **Anchor.** A utility-scale hub whose last SIGNED setpoint still has a live lease (`og.verdict` PASS for
  the engine's batch, lease = that batch's `expires_at`) steps from that setpoint; otherwise from its
  telemetry `p_kw`. The same rule as `g04_anchor_kw`.
- **Step.** At most `RAMP_SAFETY_FACTOR` x ramp x ONE cycle from the anchor, however long since the
  signature (within the guardian's bound whether it uses one cycle or the elapsed time as dt).
- **Veto.** A PARTLY_VETOED/VETOED batch drops the signed anchor of every hub in it: they re-anchor to
  telemetry (after a guardian restart its in-memory signed record is gone, so both sides meet there).
- **Lapsed lease.** Telemetry.
- **Safe stop.** While an `og.stop_event` ENGAGE covers a hub (FLEET, its BANK or ZONE --
  `core.manual_targets.stop_covers`), its bank gets no proposal at all and its signed anchor is dropped.
  After the RELEASE the hub anchors at 0 kW (where the stop left it) until a telemetry sample newer than
  the release arrives, then at telemetry.

The engine learns verdicts for its own utility-scale batches at the start of the next cycle
(`refresh`), one bounded read and only while such batches are pending.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any
from uuid import UUID

from opengrid.core.manual_targets import stop_covers
from opengrid.core.timeutil import to_utc

logger = logging.getLogger("opengrid.engine")

SIGNED_OUTCOMES = frozenset({"PASS"})
VETO_OUTCOMES = frozenset({"PARTLY_VETOED", "VETOED"})
DEFAULT_VERDICT_READ_TIMEOUT_S = 0.5


@dataclass(frozen=True, slots=True)
class SignedSetpoint:
    kw: float
    signed_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class _PendingBatch:
    setpoints: dict[str, float]
    expires_at: datetime


@dataclass(slots=True)
class RampAnchors:
    """Per utility-scale hub: its last signed setpoint, the engine's batches awaiting a verdict, and the
    hubs held at 0 kW after a safe stop until fresh telemetry (see the module docstring)."""

    signed: dict[str, SignedSetpoint] = field(default_factory=dict)
    pending: dict[UUID, _PendingBatch] = field(default_factory=dict)
    #: hub -> the stop event time after which telemetry must be newer before it is trusted again.
    zero_until_telemetry_after: dict[str, datetime] = field(default_factory=dict)
    #: hubs currently under an engaged safe stop.
    stopped: set[str] = field(default_factory=set)
    #: every utility-scale hub the engine has ramped (the only hubs this state tracks) -> its bank.
    utility_hubs: dict[str, str | None] = field(default_factory=dict)

    def anchor_kw(self, hub: Any, now: datetime) -> float | None:
        """The kW this hub's next step starts from (`None` = unknown: no ramp limit applies)."""
        hub_id = str(hub.hub_id)
        bank = getattr(hub, "bank_id", None)
        self.utility_hubs[hub_id] = str(bank) if bank is not None else None
        signed = self.signed.get(hub_id)
        if signed is not None:
            if to_utc(signed.expires_at) > to_utc(now):
                return signed.kw
            del self.signed[hub_id]  # lease lapsed: telemetry
        after = self.zero_until_telemetry_after.get(hub_id)
        if after is not None:
            seen = getattr(hub, "last_seen_at", None)
            if seen is None or to_utc(seen) <= to_utc(after):
                return 0.0
            del self.zero_until_telemetry_after[hub_id]
        p_kw = getattr(hub, "p_kw", None)
        return float(p_kw) if p_kw is not None else None

    def record_proposal(
        self, batch_id: UUID, items: Iterable[Mapping[str, Any]], expires_at: datetime
    ) -> None:
        """Remember what a batch asks of utility-scale hubs (a hub applies its LAST item in a batch)."""
        setpoints: dict[str, float] = {}
        for item in items:
            hub_id = str(item.get("hub_id"))
            if hub_id in self.utility_hubs and item.get("p_kw_setpoint") is not None:
                setpoints[hub_id] = float(item["p_kw_setpoint"])
        if setpoints:
            self.pending[batch_id] = _PendingBatch(setpoints, expires_at)

    def apply_verdicts(self, outcomes: Mapping[UUID, str], now: datetime) -> None:
        """PASS: each hub's setpoint in that batch is its signed anchor until the batch's lease ends. A veto:
        every hub in the batch re-anchors to telemetry. A batch whose lease ended with no verdict is dropped."""
        for batch_id, outcome in outcomes.items():
            batch = self.pending.pop(batch_id, None)
            if batch is None:
                continue
            if outcome in SIGNED_OUTCOMES:
                for hub_id, kw in batch.setpoints.items():
                    if hub_id not in self.stopped:
                        self.signed[hub_id] = SignedSetpoint(kw, now, batch.expires_at)
            elif outcome in VETO_OUTCOMES:
                for hub_id in batch.setpoints:
                    self.signed.pop(hub_id, None)
        for batch_id in [b for b, p in self.pending.items() if to_utc(p.expires_at) <= to_utc(now)]:
            del self.pending[batch_id]

    def apply_stops(
        self,
        latest_by_scope: Mapping[tuple[str, str], tuple[str, datetime]],
        *,
        zone_of_bank: Callable[[str], str | None],
    ) -> None:
        """Mark each tracked hub stopped (engaged) or held at 0 after a release (see the module docstring).
        `latest_by_scope`: each stop scope's latest `(action, at)` (`core.manual_targets.latest_stop_by_scope`)."""
        stopped: set[str] = set()
        for hub_id, bank in self.utility_hubs.items():
            zone = zone_of_bank(bank) if bank is not None else None
            latest_release: datetime | None = None
            for (kind, ref), (action, at) in latest_by_scope.items():
                if not stop_covers(kind, ref, bank=bank, zone=zone):
                    continue
                if action == "ENGAGE":
                    stopped.add(hub_id)
                elif latest_release is None or to_utc(at) > to_utc(latest_release):
                    latest_release = at
            if hub_id in stopped:
                self.signed.pop(hub_id, None)
            elif latest_release is not None and hub_id in self.stopped:
                # Released since the last cycle: 0 kW until telemetry newer than the release.
                self.zero_until_telemetry_after[hub_id] = latest_release
        for hub_id in stopped - self.stopped:
            logger.warning("safe stop engaged: no proposals, ramp anchor dropped", extra={"hub_id": hub_id})
        self.stopped = stopped

    async def refresh(
        self,
        read_outcomes: Callable[[list[UUID]], Any],
        now: datetime,
        *,
        timeout_s: float = DEFAULT_VERDICT_READ_TIMEOUT_S,
    ) -> None:
        """Read the verdicts of the pending utility-scale batches (bounded; unreadable = try next cycle)."""
        if not self.pending:
            return
        try:
            outcomes = await asyncio.wait_for(read_outcomes(list(self.pending)), timeout=timeout_s)
        except Exception:
            logger.warning("verdicts for ramp anchors unreadable this cycle", exc_info=True)
            outcomes = {}
        self.apply_verdicts(outcomes, now)


def stopped_banks(
    bank_ids: Iterable[str],
    latest_by_scope: Mapping[tuple[str, str], tuple[str, datetime]],
    *,
    zone_of_bank: Callable[[str], str | None],
) -> set[str]:
    """The banks among `bank_ids` under an engaged safe stop (no proposals for them)."""
    engaged = [(kind, ref) for (kind, ref), (action, _at) in latest_by_scope.items() if action == "ENGAGE"]
    if not engaged:
        return set()
    return {
        b
        for b in bank_ids
        if any(stop_covers(kind, ref, bank=b, zone=zone_of_bank(b)) for kind, ref in engaged)
    }
