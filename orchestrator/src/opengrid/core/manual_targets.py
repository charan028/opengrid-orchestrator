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
"""

from __future__ import annotations

import logging
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import datetime
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
