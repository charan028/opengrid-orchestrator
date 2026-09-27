"""ERCOT AS deployment dispatch instructions, as the MMS/EWS simulator publishes them (D-35).

ERCOT deploys a QSE's awarded ancillary services (ECRS, RRS, Reg-Up/Down, Non-Spin) in real time. For an
Energy Storage Resource the energy itself follows SCED base points, but the deployment is announced to
the QSE as a dispatch instruction that names the resource, the AS type, the deployed MW and the
start/ramp/end, and ERCOT later RECALLS it with another instruction. The QSE must acknowledge each one
(accept, or reject with a reason). The simulator models exactly that surface on the EWS `VDIs` noun
(protocol-adapters.md S6.6): `get VDIs` lists instructions not yet acknowledged, `change VDIs`
acknowledges one.

Delivery is deliberately imperfect, like a real market interface:

- **latency/jitter**: an instruction becomes visible `latency_s` (uniform in the configured range) after
  it is issued, so a poller sees it on the next poll or the one after;
- **duplicates**: with probability `duplicate_probability` an instruction is delivered once more AFTER it
  was acknowledged (an at-least-once transport re-sending);
- **out-of-order**: with probability `reorder_probability` a batch is returned in reverse issue order.

Scenario helpers (`publish_*`) build the normal and abnormal cases the control plane injects. A malformed
instruction carries raw field overrides (`malformed_fields`) that are rendered verbatim.

Nothing here knows the orchestrator: an award is only (resource, AS type, MW) as the sim registers it.
"""

from __future__ import annotations

import random
import uuid
from dataclasses import dataclass, field
from datetime import datetime, timedelta

__all__ = ["AsDispatchBook", "AsInstruction", "DeliveryProfile"]


@dataclass(frozen=True)
class DeliveryProfile:
    """How imperfect delivery is. The all-zero default delivers instantly, once, in order."""

    latency_s: tuple[float, float] = (0.0, 0.0)
    duplicate_probability: float = 0.0
    reorder_probability: float = 0.0
    seed: int = 0


@dataclass
class AsInstruction:
    """One dispatch instruction. `recall_of` names the deployment instruction a recall ends."""

    mrid: str
    resource: str
    instruction_type: str
    as_type: str | None
    mw: float | None
    start: datetime
    end: datetime | None
    ramp_minutes: int | None
    recall_of: str | None
    issued_at: datetime
    visible_at: datetime
    text: str = ""
    malformed_fields: dict[str, str] = field(default_factory=dict)
    acknowledged: bool = False
    response: str | None = None  # ACCEPTED | REJECTED
    response_reason: str | None = None
    redeliver_once: bool = False

    def fields(self) -> dict[str, str]:
        """The `Details` elements as text, malformed overrides applied last."""
        values: dict[str, str] = {"instructionType": self.instruction_type}
        if self.as_type is not None:
            values["asType"] = self.as_type
        if self.mw is not None:
            values["mw"] = f"{self.mw:g}"
        values["startTime"] = self.start.isoformat(timespec="seconds")
        if self.end is not None:
            values["endTime"] = self.end.isoformat(timespec="seconds")
        if self.ramp_minutes is not None:
            values["rampMinutes"] = str(self.ramp_minutes)
        if self.recall_of is not None:
            values["recallOf"] = self.recall_of
        values.update(self.malformed_fields)
        return values


@dataclass
class AsDispatchBook:
    """The instructions the simulated ERCOT has issued to one QSE, with their acknowledgements."""

    qse_code: str
    profile: DeliveryProfile = field(default_factory=DeliveryProfile)
    instructions: dict[str, AsInstruction] = field(default_factory=dict)
    _rng: random.Random = field(init=False)

    def __post_init__(self) -> None:
        self._rng = random.Random(self.profile.seed)  # noqa: S311 -- simulation jitter, not security

    def new_mrid(self) -> str:
        return f"{self.qse_code}.VDI.{uuid.uuid4().hex[:12]}"

    def publish(
        self,
        *,
        resource: str,
        instruction_type: str,
        issued_at: datetime,
        as_type: str | None = None,
        mw: float | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        ramp_minutes: int | None = None,
        recall_of: str | None = None,
        text: str = "",
        malformed_fields: dict[str, str] | None = None,
        mrid: str | None = None,
    ) -> AsInstruction:
        low, high = self.profile.latency_s
        instruction = AsInstruction(
            mrid=mrid or self.new_mrid(),
            resource=resource,
            instruction_type=instruction_type,
            as_type=as_type,
            mw=mw,
            start=start or issued_at,
            end=end,
            ramp_minutes=ramp_minutes,
            recall_of=recall_of,
            issued_at=issued_at,
            visible_at=issued_at + timedelta(seconds=self._rng.uniform(low, high)),
            text=text,
            malformed_fields=dict(malformed_fields or {}),
        )
        self.instructions[instruction.mrid] = instruction
        return instruction

    def pending(self, now: datetime) -> list[AsInstruction]:
        """What `get VDIs` returns now: visible and unacknowledged, plus any one-off redelivery."""
        out: list[AsInstruction] = []
        for instruction in self.instructions.values():
            if instruction.visible_at > now:
                continue
            if not instruction.acknowledged:
                out.append(instruction)
            elif instruction.redeliver_once:
                instruction.redeliver_once = False
                out.append(instruction)
        out.sort(key=lambda i: i.issued_at)
        if len(out) > 1 and self._rng.random() < self.profile.reorder_probability:
            out.reverse()
        return out

    def acknowledge(self, mrid: str, *, accepted: bool, reason: str | None) -> bool:
        """Record the QSE's answer. A first acknowledgement may schedule one duplicate redelivery."""
        instruction = self.instructions.get(mrid)
        if instruction is None:
            return False
        if not instruction.acknowledged:
            duplicate = self._rng.random() < self.profile.duplicate_probability
            instruction.redeliver_once = instruction.redeliver_once or duplicate
        instruction.acknowledged = True
        instruction.response = "ACCEPTED" if accepted else "REJECTED"
        instruction.response_reason = reason
        return True

    def force_duplicate(self, mrid: str) -> bool:
        """Deliver `mrid` once more even if it is already acknowledged (the duplicate scenario)."""
        instruction = self.instructions.get(mrid)
        if instruction is None:
            return False
        instruction.redeliver_once = True
        return True

    def latest_deployment(self, resource: str, as_type: str) -> AsInstruction | None:
        """The newest DEPLOY_AS for this resource and AS type (what a recall ends)."""
        deployments = [
            i
            for i in self.instructions.values()
            if i.instruction_type == "DEPLOY_AS" and i.resource == resource and i.as_type == as_type
        ]
        return max(deployments, key=lambda i: i.issued_at, default=None)

    def responses(self) -> list[dict[str, object]]:
        """Admin view: every instruction with its acknowledgement."""
        return [
            {
                "mrid": i.mrid,
                "resource": i.resource,
                "instruction_type": i.instruction_type,
                "as_type": i.as_type,
                "mw": i.mw,
                "issued_at": i.issued_at.isoformat(),
                "acknowledged": i.acknowledged,
                "response": i.response,
                "reason": i.response_reason,
            }
            for i in sorted(self.instructions.values(), key=lambda i: i.issued_at)
        ]
