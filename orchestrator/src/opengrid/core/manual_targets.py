"""Manual operator targets (MANUAL_TARGET trace events): the one parser both og-api (listing and cancel
checks) and og-engine (the G-04 manual ramp, `opengrid.engine.manual`) use. Pure: no I/O, no engine imports.

Payload contract (`og.trace.payload`, `event_class='MANUAL_TARGET'`, written by the API):

    {"hub_ids": ["hub-00012", ...], "p_kw_command": -5.0, "sign_convention": "+charge/-discharge",
     "issued_at": "<ISO-8601>", "expires_at": "<ISO-8601>", "proposer": "og-op-a", "reason": "..."}

and a cancel row: the same hubs with `"cancels": "<original trace id>"` (and `expires_at == issued_at`).

ONE sign convention end to end: **+charge / -discharge** -- the command item `p_kw_setpoint`
(`interfaces/mqtt/command_batch.schema.json`), hub telemetry `p_kw` (`interfaces/mqtt/telemetry.schema.json`)
and `core.physics.soc_step`. A row with any other `sign_convention` is refused (fail closed); `p_kw_target` is
read as the older name of `p_kw_command`.

Rules: rows in `created_at` order; the newest target per hub wins; a cancel ends the named target only on
hubs where it is still the newest; expired and malformed rows are ignored.
`effective_targets` is the ONE status rule (engine dispatch, the API's target list, the guardian's
R-OPERATOR-OVERRIDE evidence): per hub, its newest target and whether it is ACTIVE, EXPIRED, CANCELLED_BY_OPERATOR,
CANCELLED_BY_SAFE_STOP (with the stop id: an og.stop_event ENGAGE covering the hub's bank, zone or the fleet,
issued at or after the target, or still engaged) or CANCELLED_LATE_RECORD (the engine refused a target that
reached og.trace long after it was issued, e.g. a trace-journal replay of a write the API reported as failed).
"""

from __future__ import annotations

import logging
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
from enum import StrEnum
from typing import Any

from opengrid.core.timeutil import to_utc

logger = logging.getLogger(__name__)

MANUAL_TARGET_EVENT = "MANUAL_TARGET"
#: The one sign convention of manual targets, command items, telemetry `p_kw` and `core.physics`.
SIGN_CONVENTION = "+charge/-discharge"
#: How far back a target can still be live (targets are minutes long; this bounds the read).
LIVE_WINDOW_HOURS = 24

#: The rows `parse_targets` expects, in order: `(trace_id, payload, created_at)`. The literal repeats
#: MANUAL_TARGET_EVENT and LIVE_WINDOW_HOURS (a test keeps them in step).
MANUAL_TARGET_ROWS_SQL = """
SELECT trace_id, payload, created_at FROM og.trace
WHERE event_class = 'MANUAL_TARGET' AND created_at > now() - interval '24 hours'
ORDER BY created_at
"""


@dataclass(frozen=True, slots=True)
class ManualTarget:
    """A hub's live operator target: `p_kw_target` in +charge / -discharge."""

    hub_id: str
    p_kw_target: float
    issued_at: datetime
    expires_at: datetime
    trace_id: str
    proposer: str = ""


def parse_targets(
    rows: Iterable[tuple[Any, Mapping[str, Any], datetime]], now: datetime
) -> dict[str, ManualTarget]:
    """The live target per hub from MANUAL_TARGET rows (see the module rules)."""
    targets: dict[str, ManualTarget] = {}
    for trace_id, payload, created_at in rows:
        if payload.get("cancels"):
            cancelled = str(payload["cancels"])
            for hub_id in [str(h) for h in payload.get("hub_ids") or []]:
                held = targets.get(hub_id)
                if held is not None and held.trace_id == cancelled:
                    targets.pop(hub_id)
            continue
        convention = payload.get("sign_convention", SIGN_CONVENTION)
        if convention != SIGN_CONVENTION:
            logger.error(
                "MANUAL_TARGET with an unsupported sign convention refused",
                extra={"trace_id": str(trace_id), "sign_convention": convention},
            )
            continue
        try:
            raw = payload["p_kw_command"] if "p_kw_command" in payload else payload["p_kw_target"]
            target = float(raw)
            expires = datetime.fromisoformat(str(payload["expires_at"]))
            issued = datetime.fromisoformat(str(payload.get("issued_at") or created_at.isoformat()))
            hub_ids = [str(h) for h in payload["hub_ids"]]
        except (KeyError, TypeError, ValueError):
            logger.warning("malformed MANUAL_TARGET ignored", extra={"trace_id": str(trace_id)})
            continue
        for hub_id in hub_ids:
            held = targets.get(hub_id)
            if held is None or to_utc(issued) >= to_utc(held.issued_at):
                targets[hub_id] = ManualTarget(
                    hub_id, target, issued, expires, str(trace_id), str(payload.get("proposer", ""))
                )
    return {h: t for h, t in targets.items() if to_utc(t.expires_at) > to_utc(now)}


#: `og.stop_event` rows `effective_targets` expects, in order: `(stop_event_id, scope_kind, scope_ref, action,
#: created_at)`, oldest first: every stop event of the last 24 h (a target lives at most that long, so an
#: ENGAGE at or after its issue is among them) PLUS the latest event of every scope whatever its age -- a
#: stop engaged more than 24 h ago and never released is still in force and must still cancel targets.
STOP_EVENT_ROWS_SQL = """
SELECT s.stop_event_id, s.scope_kind, s.scope_ref, s.action, s.created_at FROM og.stop_event s
WHERE s.created_at > now() - interval '24 hours'
   OR s.created_at = (
       SELECT max(l.created_at) FROM og.stop_event l
       WHERE l.scope_kind = s.scope_kind AND l.scope_ref = s.scope_ref
   )
ORDER BY s.created_at
"""
#: A cancel row's `cancel_kind` when the engine refused a target recorded too late (not the operator).
CANCEL_KIND_LATE_RECORD = "LATE_RECORD"


class TargetStatus(StrEnum):
    ACTIVE = "ACTIVE"
    EXPIRED = "EXPIRED"
    CANCELLED_BY_OPERATOR = "CANCELLED_BY_OPERATOR"
    CANCELLED_BY_SAFE_STOP = "CANCELLED_BY_SAFE_STOP"
    CANCELLED_LATE_RECORD = "CANCELLED_LATE_RECORD"


@dataclass(frozen=True, slots=True)
class TargetState:
    """A hub's newest manual target and its status. `stop_event_id` names the safe stop that cancelled it;
    `cancelled_by` the cancel row's trace id."""

    target: ManualTarget
    status: TargetStatus
    stop_event_id: str | None = None
    cancelled_by: str | None = None


def _stop_rows(stops: Iterable[tuple[Any, ...]]) -> list[tuple[str, str, str, str, datetime]]:
    rows: list[tuple[str, str, str, str, datetime]] = []
    for row in stops:
        if len(row) == 5:
            stop_id, kind, ref, action, at = row
        else:  # (scope_kind, scope_ref, action, created_at) without an id
            kind, ref, action, at = row
            stop_id = ""
        rows.append((str(stop_id), str(kind), str(ref), str(action), at))
    return rows


def stop_covers(kind: str, ref: str, *, bank: str | None, zone: str | None) -> bool:
    """Whether a stop scope `(kind, ref)` covers a hub of `bank` in `zone`: FLEET, its BANK or its ZONE."""
    return kind == "FLEET" or (kind == "BANK" and ref == bank) or (kind == "ZONE" and ref == zone)


def latest_stop_by_scope(stops: Iterable[tuple[Any, ...]]) -> dict[tuple[str, str], tuple[str, datetime]]:
    """Each stop scope's latest `(action, created_at)` from `STOP_EVENT_ROWS_SQL` rows (oldest first): a
    scope whose latest action is ENGAGE is engaged now."""
    return {(kind, ref): (action, at) for _id, kind, ref, action, at in _stop_rows(stops)}


def _covering_stop(
    target: ManualTarget,
    stops: list[tuple[str, str, str, str, datetime]],
    *,
    bank: str | None,
    zone: str | None,
) -> str | None:
    """The id of the safe stop that cancels `target`: the first covering ENGAGE at or after it was issued,
    else a covering stop still engaged (its scope's latest action is ENGAGE). `None`: no stop applies."""
    for stop_id, kind, ref, action, at in stops:
        if (
            action == "ENGAGE"
            and stop_covers(kind, ref, bank=bank, zone=zone)
            and to_utc(at) >= to_utc(target.issued_at)
        ):
            return stop_id or "unknown"
    latest: dict[tuple[str, str], tuple[str, str]] = {}
    for stop_id, kind, ref, action, _at in stops:
        latest[(kind, ref)] = (action, stop_id)
    for (kind, ref), (action, stop_id) in latest.items():
        if action == "ENGAGE" and stop_covers(kind, ref, bank=bank, zone=zone):
            return stop_id or "unknown"
    return None


def effective_targets(
    rows: Iterable[tuple[Any, Mapping[str, Any], datetime]],
    stops: Iterable[tuple[Any, ...]],
    now: datetime,
    *,
    bank_of_hub: Callable[[str], str | None],
    zone_of_bank: Callable[[str], str | None],
) -> dict[str, TargetState]:
    """Per hub, its newest target (rows in `created_at` order) with its status (module docstring). Order of
    precedence: an operator (or late-record) cancel, then a safe stop, then expiry; otherwise ACTIVE. Rows
    with a foreign sign convention or malformed fields are refused and not listed."""
    newest: dict[str, ManualTarget] = {}
    cancelled: dict[str, tuple[TargetStatus, str]] = {}
    for trace_id, payload, created_at in rows:
        if payload.get("cancels"):
            target_id = str(payload["cancels"])
            status = (
                TargetStatus.CANCELLED_LATE_RECORD
                if payload.get("cancel_kind") == CANCEL_KIND_LATE_RECORD
                else TargetStatus.CANCELLED_BY_OPERATOR
            )
            for hub_id in [str(h) for h in payload.get("hub_ids") or []]:
                held = newest.get(hub_id)
                if held is not None and held.trace_id == target_id and hub_id not in cancelled:
                    cancelled[hub_id] = (status, str(trace_id))
            continue
        parsed = parse_targets([(trace_id, payload, created_at)], datetime.min.replace(tzinfo=now.tzinfo))
        for hub_id, target in parsed.items():
            held = newest.get(hub_id)
            if held is None or to_utc(target.issued_at) >= to_utc(held.issued_at):
                newest[hub_id] = target
                cancelled.pop(hub_id, None)
    stop_rows = _stop_rows(stops)
    out: dict[str, TargetState] = {}
    for hub_id, target in newest.items():
        if hub_id in cancelled:
            status, by = cancelled[hub_id]
            out[hub_id] = TargetState(target, status, cancelled_by=by)
            continue
        bank = bank_of_hub(hub_id)
        stop_id = _covering_stop(
            target, stop_rows, bank=bank, zone=zone_of_bank(bank) if bank is not None else None
        )
        if stop_id is not None:
            out[hub_id] = TargetState(target, TargetStatus.CANCELLED_BY_SAFE_STOP, stop_event_id=stop_id)
        elif to_utc(target.expires_at) <= to_utc(now):
            out[hub_id] = TargetState(target, TargetStatus.EXPIRED)
        else:
            out[hub_id] = TargetState(target, TargetStatus.ACTIVE)
    return out


def active_targets(states: Mapping[str, TargetState]) -> dict[str, ManualTarget]:
    """The targets `effective_targets` found ACTIVE (what the engine dispatches, and G-19's evidence)."""
    return {h: s.target for h, s in states.items() if s.status is TargetStatus.ACTIVE}
