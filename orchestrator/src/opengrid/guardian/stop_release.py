"""K8 stop RELEASE, the guardian's half (02a S6.5, interfaces/crypto.md S2.3): pure checks and the signed
event builder. No I/O -- `GuardianService.evaluate_and_sign_stop_release` supplies the guardian's own
reads, and `og-safestop` publishes what the guardian signs (the stop-only key can never sign a RELEASE).

End-to-end path:

1. og-api, operator A: request a release of a scope (step 1 of 2). Operator B (a different person):
   approve it (step 2). og-api records ONE `og.operator_action` row: action_kind `SAFE_STOP_RELEASE`,
   tier `TIER2`, `operator_ref`=A, `approver_ref`=B, `confirmed_at`=approval time, `target_ref`
   `<SCOPE_KIND>:<scope_ref>`, the reason, and the `trace_id` of its own OPERATOR_ACTION trace row.
2. og-guardian polls those rows and signs a RELEASE only if `check_stop_release` passes on its own reads.
   It signs one RELEASE per outstanding ENGAGE on the scope, reusing that ENGAGE's `stop_id`, so the
   RELEASE is published on the ENGAGE's own retained topic and replaces it (a late-joining hub never
   sees a stale ENGAGE). The signed events are traced before anything else happens (K10).
3. og-safestop receives each signed event, verifies the guardian signature and Tier-2 fields itself,
   publishes it retained, then records the `og.stop_event` RELEASE row.
4. The hub verifies the guardian signature and the two-person fields before releasing.
"""

from __future__ import annotations

from collections.abc import Collection, Iterable
from datetime import datetime

from opengrid.core.crypto import sign_payload
from opengrid.core.models.mqtt import StopEvent
from opengrid.guardian.checks import CheckOutcome
from opengrid.guardian.ports import EngagedStop, ReleaseRequest
from opengrid.safestop.events import to_wire_scope

RULE_ID = "K8-RELEASE"

#: Utility/ISO instruction kinds that keep a stop's reason in force (K5): a scope under ESTOP/BLOCK is
#: never released by an operator request.
_BLOCKING_INSTRUCTION_KINDS = frozenset({"ESTOP", "BLOCK"})


def _same_person(a: str, b: str) -> bool:
    return a.strip().casefold() == b.strip().casefold()


def check_stop_release(
    request: ReleaseRequest,
    *,
    now: datetime,
    engaged: list[EngagedStop],
    active_instruction_kinds: Iterable[str],
    authorised_operators: Collection[str],
    approval_max_age_s: float,
    max_clock_skew_s: float,
    request_traced: bool,
) -> CheckOutcome:
    """Every precondition for signing a RELEASE, on the guardian's own reads. Fail closed throughout.

    - operator authorisation: an allow-list is configured, requester and approver are both on it, and
      they are two different people (Tier 2);
    - freshness: approved after it was requested, not in the future, and recently (a stale approval
      cannot release a stop engaged later);
    - K10: og-api's trace row for the request exists;
    - there is something to release, and every outstanding ENGAGE predates the approval;
    - the stop's reason has cleared: it was not a utility stop, and no ESTOP/BLOCK instruction is active
      on any bank in the scope."""
    allowed = {op.strip().casefold() for op in authorised_operators if op.strip()}

    def refuse(reason: str) -> CheckOutcome:
        return CheckOutcome(RULE_ID, False, reason, hub_id=f"{request.scope_kind}:{request.scope_ref}")

    if not allowed:
        return refuse("NO_AUTHORISED_OPERATORS_CONFIGURED")
    approver = (request.approved_by or "").strip()
    if not approver:
        return refuse("NOT_APPROVED")
    if _same_person(request.requested_by, approver):
        return refuse("SAME_OPERATOR")
    if request.requested_by.strip().casefold() not in allowed or approver.casefold() not in allowed:
        return refuse("OPERATOR_NOT_AUTHORISED")
    approved_at = request.approved_at
    if (
        approved_at is None
        or approved_at < request.requested_at
        or (approved_at - now).total_seconds() > max_clock_skew_s
        or (now - approved_at).total_seconds() > approval_max_age_s
    ):
        return refuse("APPROVAL_STALE")
    if not request_traced:
        return refuse("REQUEST_NOT_TRACED")
    if not engaged:
        return refuse("NOT_ENGAGED")
    if any(stop.engaged_at > approved_at for stop in engaged):
        return refuse("STOP_ENGAGED_AFTER_APPROVAL")
    if any(stop.initiator_kind == "UTILITY" for stop in engaged):
        return refuse("UTILITY_STOP_NOT_OPERATOR_RELEASABLE")
    if any(kind in _BLOCKING_INSTRUCTION_KINDS for kind in active_instruction_kinds):
        return refuse("STOP_REASON_ACTIVE_L2_INSTRUCTION")
    return CheckOutcome.passed(RULE_ID)


def build_release_events(
    request: ReleaseRequest, engaged: list[EngagedStop], *, seed: bytes, key_id: str, issued_at: datetime
) -> list[StopEvent]:
    """One guardian-signed RELEASE per outstanding ENGAGE, each reusing the ENGAGE's `stop_id` (so it
    lands on, and replaces, that ENGAGE's retained topic). Signed over `StopEvent.signing_payload()`,
    i.e. every field but key_id/signature (crypto.md S2.3)."""
    wire_scope, scope_id = to_wire_scope(request.scope_kind, request.scope_ref)
    events: list[StopEvent] = []
    for stop in engaged:
        unsigned = StopEvent(
            stop_id=stop.stop_id,
            scope=wire_scope,
            scope_id=scope_id,
            action="RELEASE",
            reason=request.reason,
            issued_by=request.requested_by.strip(),
            issued_at=issued_at,
            approver_ref=(request.approved_by or "").strip(),
            key_id=key_id,
            signature="",
        )
        events.append(
            unsigned.model_copy(update={"signature": sign_payload(seed, unsigned.signing_payload())})
        )
    return events
