"""Manual operator setpoints, ramped by the engine (live finding 2026-09-26: G-04 bounds a hub's step to
about p_kw/180 per 2 s cycle, so a one-shot manual command was always vetoed).

The API records the operator's confirmed intent as a K10 trace event (`event_class='MANUAL_TARGET'`,
payload below) instead of proposing a one-shot batch. The engine reads the live targets every cycle and,
until each expires, emits one guardian-checked item per hub stepping from its measured power toward the
target within G-04's rate (the same `_ramped_setpoint_kw` the dispatch items use), then holds it there. Each
item carries R-MANUAL-RAMP. While a hub has a live target it is operator-owned: the allocator treats it as
unavailable (its committed kW moves to other hubs of the same obligation, K13 substitution).

The payload contract, the sign convention and the parser live in `opengrid.core.manual_targets` (one
implementation, shared with og-api). Summary (`og.trace.payload` of a MANUAL_TARGET event):

    {"hub_ids": ["hub-00012", ...], "p_kw_command": -5.0, "sign_convention": "+charge/-discharge",
     "expires_at": "<ISO-8601>", "issued_at": "<ISO-8601>", "proposer": "og-op-a", "reason": "..."}

ONE sign convention end to end, the command convention: **+charge / -discharge**. It is the convention of
the command item `p_kw_setpoint` (`interfaces/mqtt/command_batch.schema.json`, the fixtures' -3.2
discharge), of hub telemetry `p_kw` (`interfaces/mqtt/telemetry.schema.json`: "+charge / -discharge"; the sim
publishes its applied power as is), and of `core.physics.soc_step` (a positive p raises SoC). The engine
steps from the measured telemetry `p_kw` toward the target in that same convention, so no conversion is
needed anywhere. `sign_convention` must be "+charge/-discharge" when present; any other value is refused
(the target is ignored, fail closed). `p_kw_target` is accepted as the older name of `p_kw_command`. A bank selection is resolved to its
hubs by the API. The newest target per hub wins (a new target overrides). A safe stop (an `og.stop_event`
ENGAGE covering the hub's bank, zone or the fleet at or after the target was issued) cancels the target,
and no manual item is emitted for a scope while a stop is engaged there.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from opengrid.core.manual_targets import (
    CANCEL_KIND_LATE_RECORD,
    MANUAL_TARGET_EVENT,
    MANUAL_TARGET_ROWS_SQL,
    SIGN_CONVENTION,
    STOP_EVENT_ROWS_SQL,
    ManualTarget,
    TargetStatus,
    active_targets,
    effective_targets,
    parse_targets,
)
from opengrid.core.reasons import R_MANUAL_RAMP
from opengrid.core.timeutil import to_utc

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_S = 2.0
#: A target first seen later than this after it was issued is refused (late record, e.g. journal replay).
DEFAULT_MAX_FIRST_SIGHT_LAG_S = 30.0

TraceAppend = Callable[[str, str, str, dict[str, Any]], Awaitable[object]]

_TARGETS_SQL = MANUAL_TARGET_ROWS_SQL
_STOPS_SQL = STOP_EVENT_ROWS_SQL


class ManualTargetSource:
    """Reads MANUAL_TARGET rows and safe stops from Postgres at most every `refresh_s`, and keeps the ACTIVE
    targets of `core.manual_targets.effective_targets` (the one status rule the API list and the guardian
    use too); keeps the last good set when unreadable.

    Late records (review R3.4): a target the engine first sees more than `max_first_sight_lag_s` after it was
    issued -- e.g. a trace-journal replay of a write the API reported as failed (503 "not recorded") -- is
    never activated: the engine appends a cancel row (`cancel_kind` LATE_RECORD), so every reader shows it
    CANCELLED_LATE_RECORD. Targets already live at the engine's first read are accepted (baseline)."""

    def __init__(
        self,
        pool: Any,
        *,
        bank_of_hub: Callable[[str], str | None],
        zone_of_bank: Callable[[str], str | None],
        refresh_s: float = DEFAULT_REFRESH_S,
        trace: TraceAppend | None = None,
        max_first_sight_lag_s: float = DEFAULT_MAX_FIRST_SIGHT_LAG_S,
    ) -> None:
        self._pool = pool
        self._bank_of_hub = bank_of_hub
        self._zone_of_bank = zone_of_bank
        self._refresh_s = refresh_s
        self._trace = trace
        self._max_lag_s = max_first_sight_lag_s
        self._loaded_at: datetime | None = None
        self._targets: dict[str, ManualTarget] = {}
        self._seen: set[str] | None = None  # trace ids seen; None until the first (baseline) read
        self._refused: set[str] = set()

    def active_hub_ids(self) -> frozenset[str]:
        """Hubs under a live target at the last read: operator-owned, unavailable to the allocator."""
        now = datetime.now(UTC)
        return frozenset(h for h, t in self._targets.items() if to_utc(t.expires_at) > now)

    async def targets(self, now: datetime) -> dict[str, ManualTarget]:
        if self._loaded_at is None or (now - self._loaded_at).total_seconds() >= self._refresh_s:
            self._loaded_at = now
            try:
                async with self._pool.connection() as conn, conn.cursor() as cur:
                    await cur.execute(_TARGETS_SQL)
                    target_rows = await cur.fetchall()
                    await cur.execute(_STOPS_SQL)
                    stop_rows = await cur.fetchall()
                active = active_targets(
                    effective_targets(
                        target_rows,
                        stop_rows,
                        now,
                        bank_of_hub=self._bank_of_hub,
                        zone_of_bank=self._zone_of_bank,
                    )
                )
                self._targets = await self._drop_late(active, now)
            except Exception:
                logger.warning("manual targets unreadable; keeping the last set", exc_info=True)
        return {h: t for h, t in self._targets.items() if to_utc(t.expires_at) > to_utc(now)}

    async def _drop_late(self, active: dict[str, ManualTarget], now: datetime) -> dict[str, ManualTarget]:
        ids = {t.trace_id for t in active.values()}
        if self._seen is None:
            self._seen = set(ids)  # baseline: live at start-up
            return active
        late = {
            t.trace_id
            for t in active.values()
            if t.trace_id not in self._seen
            and (to_utc(now) - to_utc(t.issued_at)).total_seconds() > self._max_lag_s
        }
        self._seen |= ids - late
        for trace_id in sorted(late - self._refused):
            hubs = sorted(h for h, t in active.items() if t.trace_id == trace_id)
            self._refused.add(trace_id)
            logger.error(
                "manual target recorded too late; refused", extra={"trace_id": trace_id, "hubs": hubs}
            )
            if self._trace is not None:
                try:
                    await self._trace(
                        f"manual-late-{trace_id}",
                        "OPERATOR_ACTION",
                        MANUAL_TARGET_EVENT,
                        {
                            "hub_ids": hubs,
                            "cancels": trace_id,
                            "cancel_kind": CANCEL_KIND_LATE_RECORD,
                            "issued_at": now.isoformat(),
                            "expires_at": now.isoformat(),
                            "proposer": "og-engine",
                            "reason": "recorded too late (not activated)",
                        },
                    )
                except Exception:
                    logger.exception("could not record a late-target refusal", extra={"trace_id": trace_id})
        return {h: t for h, t in active.items() if t.trace_id not in late and t.trace_id not in self._refused}


def manual_items(
    bank_id: str,
    targets: Mapping[str, ManualTarget],
    hubs: Iterable[Any],
    ramp: Callable[[Any, float], float],
) -> list[dict[str, object]]:
    """One item per online hub of `bank_id` with a live target: its setpoint stepped toward the target by
    `ramp(hub, target_kw)` (G-04), reason R-MANUAL-RAMP, no obligation."""
    items: list[dict[str, object]] = []
    for hub in hubs:
        target = targets.get(hub.hub_id)
        if target is None or getattr(hub, "health", "online") != "online":
            continue
        items.append(
            {
                "hub_id": hub.hub_id,
                "p_kw_setpoint": ramp(hub, target.p_kw_target),
                "reason_code": R_MANUAL_RAMP,
                "obligation_id": None,
                "obligation_granted_kw": None,
                "manual_target_trace_id": target.trace_id,
            }
        )
    return items


__all__ = [
    "MANUAL_TARGET_EVENT",
    "SIGN_CONVENTION",
    "ManualTarget",
    "ManualTargetSource",
    "TargetStatus",
    "active_targets",
    "effective_targets",
    "manual_items",
    "parse_targets",
]
