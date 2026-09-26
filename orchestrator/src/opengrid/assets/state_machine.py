"""The `og.hub_inverter_pq.asset_state` lifecycle (07-delivery/06-service-profiles-and-power-quality.md
S5.5.1-2):

    OK -> WATCH -> DEGRADED/DERATED -> QUARANTINED -> AWAITING_REPLACEMENT -> RECOMMISSIONING -> OK

Pure functions over an explicit transition table -- no I/O, no clock, no persistence (BUILD.md S5a "pure
logic separated from I/O"). `opengrid.assets.service.AssetHealthService` drives this from measured drift
observations and calibration outcomes read through `opengrid.assets.ports`, and is the only caller that
persists a resulting transition (as `og.asset_event(event_type='STATE_TRANSITION')` and a traced
`ASSET_STATE_TRANSITION` decision, K10/K11).
"""

from __future__ import annotations

from enum import StrEnum

from opengrid.guardian.pq_ports import AssetState

__all__ = ["AssetState", "DriftEvent", "InvalidAssetTransitionError", "next_asset_state"]


class DriftEvent(StrEnum):
    """Every input the asset-health lifecycle reacts to (S5.5.1, S5.5.4, S5.5.6)."""

    #: S5.5.1: a persistent (not transient) drift confirmed past the observation window.
    PERSISTENT_DRIFT_DETECTED = "PERSISTENT_DRIFT_DETECTED"
    #: A `WATCH`-worthy drift clears (no longer persistent) before any calibration attempt was made.
    WATCH_CLEARED = "WATCH_CLEARED"
    #: S5.5.4: post-calibration verification classified the attempt `CORRECTED`.
    CALIBRATION_CORRECTED = "CALIBRATION_CORRECTED"
    #: S5.5.4: `IMPROVED` -- partial correction, stays `WATCH` pending the next attempt/observation.
    CALIBRATION_IMPROVED = "CALIBRATION_IMPROVED"
    #: S5.5.4: `NO_CHANGE` -- the ladder's single recalibration attempt for this drift episode did not
    #: help; escalates without a further attempt (S5.5.4: "recalibration is attempted at most once per
    #: drift episode before escalating").
    CALIBRATION_NO_CHANGE = "CALIBRATION_NO_CHANGE"
    #: S5.5.4: `WORSE_ROLLED_BACK` -- the automatic rollback fired; counts toward escalation like a
    #: failed attempt, and the unit is immediately quarantined (S5.5.2's QUARANTINED definition
    #: explicitly includes "recalibration attempted and failed/rolled back").
    CALIBRATION_WORSE_ROLLED_BACK = "CALIBRATION_WORSE_ROLLED_BACK"
    #: S5.5.4: drift recurs within the configured window (default 14 days) of a prior `CORRECTED`
    #: outcome -- re-escalates even though the immediately-preceding attempt succeeded.
    RECURRENCE_WITHIN_WINDOW = "RECURRENCE_WITHIN_WINDOW"
    #: S5.5.5: a maintenance work order was opened with sufficient severity to pull the unit from all
    #: grid-facing dispatch immediately, ahead of a scheduled replacement.
    QUARANTINE_ESCALATED = "QUARANTINE_ESCALATED"
    #: S7.5 `REPLACE_INVERTER` control-plane action / a technician schedules the physical replacement.
    REPLACEMENT_SCHEDULED = "REPLACEMENT_SCHEDULED"
    #: S5.5.5: the physical hardware replacement has been carried out (`og.asset_event`
    #: `INVERTER_REPLACED`).
    INVERTER_REPLACED = "INVERTER_REPLACED"
    #: S5.5.6: the post-replacement verification waveform capture passes the same checks a new unit
    #: would need to pass at initial characterization.
    RECOMMISSIONING_VERIFIED = "RECOMMISSIONING_VERIFIED"
    #: S5.5.6: the verification capture fails -- the unit stays `RECOMMISSIONING` (never silently
    #: returns to dispatch, K7's "degrade, don't trip" fail-safe shape) pending another capture.
    RECOMMISSIONING_FAILED = "RECOMMISSIONING_FAILED"
    #: Operator/owner-confirmed false positive (R2 incident, 2026-09-26: `PgDriftObservationRepo` fed a
    #: stale pre-calibration offset -- see `repo.py`'s "LIVE BUG FIX" comment -- into hubs that were
    #: never actually drifting). An explicit, audited override (`AssetHealthService.record_false_
    #: positive_reset`) back to `OK`, never automatic and never available from `RECOMMISSIONING` (a
    #: physical replacement already happened there; that is not a "false positive" to undo).
    OPERATOR_FALSE_POSITIVE_RESET = "OPERATOR_FALSE_POSITIVE_RESET"


class InvalidAssetTransitionError(ValueError):
    """Raised for an event the current state does not accept -- fails closed (BUILD.md S5a "no silent
    fallbacks") rather than guessing a resulting state."""

    def __init__(self, state: AssetState, event: DriftEvent) -> None:
        super().__init__(f"asset_state {state!r} does not accept event {event!r}")
        self.state = state
        self.event = event


#: (current_state, event) -> next_state. Every transition the spec's lifecycle diagram and S5.5.1/4-6
#: narrative describe; anything not listed here is refused by `next_asset_state` (fail closed).
_TRANSITIONS: dict[tuple[AssetState, DriftEvent], AssetState] = {
    ("OK", DriftEvent.PERSISTENT_DRIFT_DETECTED): "WATCH",
    ("WATCH", DriftEvent.WATCH_CLEARED): "OK",
    ("WATCH", DriftEvent.CALIBRATION_CORRECTED): "OK",
    ("WATCH", DriftEvent.CALIBRATION_IMPROVED): "WATCH",
    ("WATCH", DriftEvent.CALIBRATION_NO_CHANGE): "DEGRADED",
    ("WATCH", DriftEvent.CALIBRATION_WORSE_ROLLED_BACK): "QUARANTINED",
    ("WATCH", DriftEvent.RECURRENCE_WITHIN_WINDOW): "DEGRADED",
    ("DEGRADED", DriftEvent.QUARANTINE_ESCALATED): "QUARANTINED",
    ("DEGRADED", DriftEvent.REPLACEMENT_SCHEDULED): "AWAITING_REPLACEMENT",
    ("QUARANTINED", DriftEvent.REPLACEMENT_SCHEDULED): "AWAITING_REPLACEMENT",
    ("AWAITING_REPLACEMENT", DriftEvent.INVERTER_REPLACED): "RECOMMISSIONING",
    ("RECOMMISSIONING", DriftEvent.RECOMMISSIONING_VERIFIED): "OK",
    ("RECOMMISSIONING", DriftEvent.RECOMMISSIONING_FAILED): "RECOMMISSIONING",
    # Operator-confirmed false positive: undoes an unwarranted WATCH/DEGRADED/QUARANTINED/AWAITING_
    # REPLACEMENT escalation back to OK. Deliberately excludes RECOMMISSIONING (S5.5.6: a physical
    # replacement already happened there, so there is nothing to call a "false positive").
    ("WATCH", DriftEvent.OPERATOR_FALSE_POSITIVE_RESET): "OK",
    ("DEGRADED", DriftEvent.OPERATOR_FALSE_POSITIVE_RESET): "OK",
    ("QUARANTINED", DriftEvent.OPERATOR_FALSE_POSITIVE_RESET): "OK",
    ("AWAITING_REPLACEMENT", DriftEvent.OPERATOR_FALSE_POSITIVE_RESET): "OK",
}


def next_asset_state(state: AssetState, event: DriftEvent) -> AssetState:
    """Advance the lifecycle by one event. Raises `InvalidAssetTransitionError` for any (state, event) pair
    the spec's lifecycle does not define -- e.g. `RECOMMISSIONING_VERIFIED` while still `OK`, or a second
    `PERSISTENT_DRIFT_DETECTED` while already `QUARANTINED` (that hub is already excluded from dispatch;
    the caller has nothing new to act on and a silent no-op would hide a bug in the caller's own state
    tracking)."""
    try:
        return _TRANSITIONS[(state, event)]
    except KeyError:
        raise InvalidAssetTransitionError(state, event) from None
