"""ogsim.utility_aen.scenarios -- the five on-demand utility scenarios, triggered from the /ogsim/
control page (catalogue owner `utility`) and run by the runtime through its `Channel`:

- `utility_call_normal`: a discharge call now; expected ACCEPTED;
- `utility_call_overlap`: a call, then a second one overlapping it; expected second REFUSED R-CALL-OVERLAP;
- `utility_call_over_cap`: a 91-minute call; expected REFUSED R-CALL-DURATION-CAP (90 min product);
- `utility_call_charge`: a POSITIVE kW (a charge instruction); expected REFUSED R-CALL-CHARGE-REFUSED;
- `utility_call_cancel_mid`: a call, then a cancel after `cancel_after_s`; expected COMPLETED.

Each run returns a `ScenarioOutcome` with the expectation and PASS/FAIL, logged by the runtime so QA can
correlate the orchestrator's response. Scenario calls clean up after themselves (cancelled at the end)
except `normal`, which runs its full duration like a real call.
"""

from __future__ import annotations

import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from ogsim.common.clock import Clock
from ogsim.utility_aen.channels.base import CallResult, CallSpec, Channel
from ogsim.utility_aen.config import PRODUCT_CAP_MIN

DEFAULT_KW = 5_000.0
DEFAULT_DURATION_MIN = 15
DEFAULT_CANCEL_AFTER_S = 60.0


@dataclass
class ScenarioOutcome:
    name: str
    expected: str
    passed: bool = False
    results: list[CallResult] = field(default_factory=list)

    def to_log(self) -> dict[str, Any]:
        return {
            "scenario": self.name,
            "expected": self.expected,
            "result": "PASS" if self.passed else "FAIL",
            "calls": [
                {
                    "call_ref": r.call_ref,
                    "state": r.state,
                    "reason_code": r.reason_code,
                    "remote_id": r.remote_id,
                }
                for r in self.results
            ],
        }


@dataclass(frozen=True)
class ScenarioContext:
    channel: Channel
    clock: Clock
    utility_id: str
    params: dict[str, Any]

    def spec(self, tag: str, *, kw: float | None = None, duration_min: int | None = None) -> CallSpec:
        magnitude = float(self.params.get("kw", DEFAULT_KW))
        return CallSpec(
            call_ref=f"{self.utility_id.lower()}-{tag}-{uuid.uuid4().hex[:12]}",
            kw=-abs(magnitude) if kw is None else kw,
            start=datetime.fromtimestamp(self.clock.now(), tz=UTC),
            duration_min=duration_min or int(self.params.get("duration_min", DEFAULT_DURATION_MIN)),
            reason=f"ogsim scenario {tag}",
        )


def _refused_with(result: CallResult, code: str) -> bool:
    return not result.accepted and result.state == "REFUSED" and result.reason_code == code


async def _cleanup(ctx: ScenarioContext, result: CallResult) -> None:
    if result.accepted:
        await ctx.channel.cancel(result.call_ref)


async def normal(ctx: ScenarioContext) -> ScenarioOutcome:
    out = ScenarioOutcome("utility_call_normal", "ACCEPTED")
    first = await ctx.channel.issue_call(ctx.spec("normal"))
    out.results.append(first)
    out.passed = first.accepted
    return out


async def overlap(ctx: ScenarioContext) -> ScenarioOutcome:
    out = ScenarioOutcome("utility_call_overlap", "second call REFUSED R-CALL-OVERLAP")
    first = await ctx.channel.issue_call(ctx.spec("overlap-a"))
    second = await ctx.channel.issue_call(ctx.spec("overlap-b"))
    out.results += [first, second]
    out.passed = first.accepted and _refused_with(second, "R-CALL-OVERLAP")
    await _cleanup(ctx, first)
    return out


async def over_cap(ctx: ScenarioContext) -> ScenarioOutcome:
    out = ScenarioOutcome("utility_call_over_cap", "REFUSED R-CALL-DURATION-CAP")
    result = await ctx.channel.issue_call(ctx.spec("over-cap", duration_min=PRODUCT_CAP_MIN + 1))
    out.results.append(result)
    out.passed = _refused_with(result, "R-CALL-DURATION-CAP")
    await _cleanup(ctx, result)
    return out


async def charge(ctx: ScenarioContext) -> ScenarioOutcome:
    out = ScenarioOutcome("utility_call_charge", "REFUSED R-CALL-CHARGE-REFUSED")
    magnitude = abs(float(ctx.params.get("kw", DEFAULT_KW)))
    result = await ctx.channel.issue_call(ctx.spec("charge", kw=magnitude))
    out.results.append(result)
    out.passed = _refused_with(result, "R-CALL-CHARGE-REFUSED")
    await _cleanup(ctx, result)
    return out


async def cancel_mid(ctx: ScenarioContext) -> ScenarioOutcome:
    out = ScenarioOutcome("utility_call_cancel_mid", "ACCEPTED, then COMPLETED after the cancel")
    first = await ctx.channel.issue_call(ctx.spec("cancel-mid"))
    out.results.append(first)
    if not first.accepted:
        return out
    await ctx.clock.sleep(float(ctx.params.get("cancel_after_s", DEFAULT_CANCEL_AFTER_S)))
    cancelled = await ctx.channel.cancel(first.call_ref)
    out.results.append(cancelled)
    out.passed = cancelled.state == "COMPLETED"
    return out


SCENARIOS: dict[str, Callable[[ScenarioContext], Awaitable[ScenarioOutcome]]] = {
    "utility_call_normal": normal,
    "utility_call_overlap": overlap,
    "utility_call_over_cap": over_cap,
    "utility_call_charge": charge,
    "utility_call_cancel_mid": cancel_mid,
}
