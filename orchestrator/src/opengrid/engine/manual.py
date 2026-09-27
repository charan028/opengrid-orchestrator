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
from collections.abc import Callable, Iterable, Mapping
from datetime import UTC, datetime
from typing import Any

from opengrid.core.manual_targets import (
    MANUAL_TARGET_EVENT,
    MANUAL_TARGET_ROWS_SQL,
    SIGN_CONVENTION,
    ManualTarget,
    parse_targets,
)
from opengrid.core.reasons import R_MANUAL_RAMP
from opengrid.core.timeutil import to_utc

logger = logging.getLogger(__name__)

DEFAULT_REFRESH_S = 2.0

_TARGETS_SQL = MANUAL_TARGET_ROWS_SQL
_STOPS_SQL = """
SELECT scope_kind, scope_ref, action, created_at FROM og.stop_event
WHERE created_at > now() - interval '24 hours'
ORDER BY created_at
"""


def apply_stops(
    targets: Mapping[str, ManualTarget],
    stops: Iterable[tuple[str, str, str, datetime]],
    *,
    bank_of_hub: Callable[[str], str | None],
    zone_of_bank: Callable[[str], str | None],
) -> dict[str, ManualTarget]:
    """Drop targets a safe stop overrides: an ENGAGE covering the hub (its bank, its zone, or the fleet)
    issued at or after the target, or a covering stop still engaged (its latest action is ENGAGE)."""
    stop_rows = [(str(k), str(r), str(a), at) for k, r, a, at in stops]
    latest: dict[tuple[str, str], str] = {}
    for kind, ref, action, _at in stop_rows:
        latest[(kind, ref)] = action
    kept: dict[str, ManualTarget] = {}
    for hub_id, target in targets.items():
        bank = bank_of_hub(hub_id)
        zone = zone_of_bank(bank) if bank is not None else None

        def covers(kind: str, ref: str, bank: str | None = bank, zone: str | None = zone) -> bool:
            return kind == "FLEET" or (kind == "BANK" and ref == bank) or (kind == "ZONE" and ref == zone)

        in_force = any(action == "ENGAGE" and covers(k, r) for (k, r), action in latest.items())
        after = any(
            a == "ENGAGE" and covers(k, r) and to_utc(at) >= to_utc(target.issued_at)
            for k, r, a, at in stop_rows
        )
        if not (in_force or after):
            kept[hub_id] = target
    return kept


class ManualTargetSource:
    """Reads live targets and stops from Postgres at most every `refresh_s`; keeps the last good set."""

    def __init__(
        self,
        pool: Any,
        *,
        bank_of_hub: Callable[[str], str | None],
        zone_of_bank: Callable[[str], str | None],
        refresh_s: float = DEFAULT_REFRESH_S,
    ) -> None:
        self._pool = pool
        self._bank_of_hub = bank_of_hub
        self._zone_of_bank = zone_of_bank
        self._refresh_s = refresh_s
        self._loaded_at: datetime | None = None
        self._targets: dict[str, ManualTarget] = {}

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
                self._targets = apply_stops(
                    parse_targets(target_rows, now),
                    stop_rows,
                    bank_of_hub=self._bank_of_hub,
                    zone_of_bank=self._zone_of_bank,
                )
            except Exception:
                logger.warning("manual targets unreadable; keeping the last set", exc_info=True)
        return {h: t for h, t in self._targets.items() if to_utc(t.expires_at) > to_utc(now)}


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
    "apply_stops",
    "manual_items",
    "parse_targets",
]
