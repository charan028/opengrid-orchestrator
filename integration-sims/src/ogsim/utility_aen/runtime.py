"""ogsim.utility_aen.runtime -- the simulated utility EMS loop.

Autonomous: on a hot day (`schedule.day_weather`) it issues the day's peak call (`schedule.plan_for_day`)
once its start arrives, then follows it with status reads until it completes or is refused. On demand:
the five scenarios (`scenarios.SCENARIOS`) run when the /ogsim/ control plane publishes a matching
`<root>/scenario/cmd` targeting this utility (or `*`). A disabled utility (D-37) never calls anything.

Everything runs on one asyncio loop; every channel call goes through the configured `Channel`, so the
same runtime drives the customer API or the grid link. Transport failures (`ChannelError`) are logged and
retried on the next tick; an orchestrator refusal is a result, not an error.
"""

from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass, field
from datetime import UTC, date, datetime
from typing import Any

from ogsim.common.clock import Clock
from ogsim.common.scenario import parse_scenario_cmd
from ogsim.utility_aen.channels.base import CallResult, CallSpec, Channel, ChannelError
from ogsim.utility_aen.config import UtilitySimConfig
from ogsim.utility_aen.scenarios import SCENARIOS, ScenarioContext, ScenarioOutcome
from ogsim.utility_aen.schedule import PlannedCall, day_weather, local_day, plan_for_day

logger = logging.getLogger(__name__)
_FINAL_STATES = frozenset({"COMPLETED", "REFUSED"})


def _log(event: str, **fields: Any) -> None:
    logger.info(json.dumps({"event": event, **fields}, default=str, sort_keys=True))


@dataclass
class DayState:
    day: date
    plan: PlannedCall | None
    issued: CallResult | None = None
    last: CallResult | None = None
    next_poll_at: float = 0.0
    skipped_reason: str | None = None


@dataclass
class UtilitySim:
    config: UtilitySimConfig
    channel: Channel | None
    clock: Clock
    today: DayState | None = None
    outcomes: list[ScenarioOutcome] = field(default_factory=list)
    _tasks: set[asyncio.Task[ScenarioOutcome | None]] = field(default_factory=set)

    @property
    def utility_id(self) -> str:
        return self.config.utility.utility_id

    @property
    def active(self) -> bool:
        return self.config.utility.enabled and self.channel is not None

    def _now(self) -> datetime:
        return datetime.fromtimestamp(self.clock.now(), tz=UTC)

    def _day_state(self, now: datetime) -> DayState:
        day = local_day(now)
        if self.today is None or self.today.day != day:
            weather = day_weather(day, self.config.weather)
            plan = plan_for_day(day, weather, self.config.schedule, utility_id=self.utility_id)
            self.today = DayState(day, plan)
            _log(
                "utility_day",
                utility=self.utility_id,
                day=day,
                high_f=weather.high_f,
                hot=weather.hot,
                planned_start=plan.start if plan else None,
                planned_kw=plan.kw if plan else None,
            )
        return self.today

    async def tick(self) -> None:
        """One scheduler step: issue the day's call when due, then follow it."""
        if not self.active or self.channel is None:
            return
        now = self._now()
        state = self._day_state(now)
        plan = state.plan
        if plan is None or state.skipped_reason is not None:
            return
        try:
            if state.issued is None and plan.start <= now < plan.end:
                await self._issue(state, plan)
            elif (
                state.issued is not None and state.issued.accepted and self.clock.now() >= state.next_poll_at
            ):
                await self._poll(state)
        except ChannelError as exc:
            _log("utility_channel_error", utility=self.utility_id, error=str(exc))

    async def _issue(self, state: DayState, plan: PlannedCall) -> None:
        assert self.channel is not None  # noqa: S101 -- guarded by tick()
        if not await self._has_reservation():
            state.skipped_reason = "no tolling reservation today"
            _log("utility_call_skipped", utility=self.utility_id, reason=state.skipped_reason)
            return
        # A late start (sim restarted mid-window) keeps the planned end: start now, shorter call.
        now = self._now()
        remaining_min = max(1, int((plan.end - now).total_seconds() // 60))
        spec = CallSpec(
            call_ref=plan.call_ref,
            kw=plan.kw,
            start=now,
            duration_min=min(plan.duration_min, remaining_min),
            reason="evening peak call",
        )
        result = await self.channel.issue_call(spec)
        state.issued = state.last = result
        state.next_poll_at = self.clock.now() + self.config.status_poll_s
        _log(
            "utility_call_issued",
            utility=self.utility_id,
            call_ref=spec.call_ref,
            kw=spec.kw,
            duration_min=spec.duration_min,
            state=result.state,
            reason_code=result.reason_code,
        )

    async def _has_reservation(self) -> bool:
        """True unless the channel can list obligations and today has none (a suspended contract, D-37)."""
        reader = getattr(self.channel, "obligations", None)
        if reader is None:
            return True
        body = await reader()
        return bool(body.get("today"))

    async def _poll(self, state: DayState) -> None:
        assert self.channel is not None and state.issued is not None  # noqa: S101 -- guarded by tick()
        if state.last is not None and state.last.state in _FINAL_STATES:
            return
        state.last = await self.channel.status(state.issued.call_ref)
        state.next_poll_at = self.clock.now() + self.config.status_poll_s
        _log(
            "utility_call_status",
            utility=self.utility_id,
            call_ref=state.last.call_ref,
            state=state.last.state,
            delivered_kw=state.last.delivered_kw,
            delivered_kwh=state.last.delivered_kwh,
            delivery_state=state.last.delivery_state,
            granted_kw=state.last.granted_kw,
            granted_kwh=state.last.granted_kwh,
        )

    def handle_scenario_cmd(self, raw: dict[str, Any]) -> bool:
        """Start a scenario for a matching `scenario/cmd`; False when it is not for this utility sim."""
        try:
            cmd = parse_scenario_cmd(raw)
        except (ValueError, KeyError):
            return False
        if cmd.catalogue_type not in SCENARIOS or cmd.target_ref not in (self.utility_id, "*"):
            return False
        if cmd.duration_s == 0:
            return True  # the control plane's "cancel" re-publish; scenarios clean up themselves
        if not self.active:
            _log(
                "utility_scenario_refused",
                utility=self.utility_id,
                scenario=cmd.catalogue_type,
                reason="utility disabled",
            )
            return True
        task = asyncio.get_running_loop().create_task(
            self.run_scenario(cmd.catalogue_type, cmd.params, cmd.id)
        )
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        return True

    async def run_scenario(
        self, name: str, params: dict[str, Any], anomaly_id: str = ""
    ) -> ScenarioOutcome | None:
        if self.channel is None:
            return None
        ctx = ScenarioContext(self.channel, self.clock, self.utility_id, dict(params))
        try:
            outcome = await SCENARIOS[name](ctx)
        except ChannelError as exc:
            _log(
                "utility_scenario_error",
                utility=self.utility_id,
                scenario=name,
                anomaly_id=anomaly_id,
                error=str(exc),
            )
            return None
        self.outcomes.append(outcome)
        _log("utility_scenario_result", utility=self.utility_id, anomaly_id=anomaly_id, **outcome.to_log())
        return outcome

    async def run(self) -> None:
        """Tick forever (the process entry point cancels this task on shutdown)."""
        if not self.config.utility.enabled:
            _log(
                "utility_disabled",
                utility=self.utility_id,
                detail="idle: no calls, no credentials read (enable it only with an active contract)",
            )
        while True:
            await self.tick()
            await self.clock.sleep(self.config.tick_s)
