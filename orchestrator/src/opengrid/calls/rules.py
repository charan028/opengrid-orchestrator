"""The call checks (D-29, D-33), pure: no I/O, no clock. Every origin -- operator, utility customer API,
grid link, ERCOT poller, market sim, scenario -- goes through these same functions via
`opengrid.calls.service.issue_call`; nothing else in the orchestrator re-implements them.

Order matters only for which reason a caller sees first; every check must pass for a call to deploy.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from opengrid.calls.models import ALLOWED_KINDS, AwardView, CallKind, CallRequest

# --- reason codes (the only place they are spelled) ---------------------------------------------------
R_NOT_FOUND = "R-CALL-NOT-FOUND"
R_NOT_DEPLOYABLE = "R-CALL-NOT-DEPLOYABLE"
R_STATE = "R-CALL-STATE"
R_NO_PRODUCT_DURATION = "R-CALL-NO-PRODUCT-DURATION"
R_DURATION_CAP = "R-CALL-DURATION-CAP"
R_OVERLAP = "R-CALL-OVERLAP"
R_CHARGE_REFUSED = "R-CALL-CHARGE-REFUSED"
R_OVER_COMMITTED = "R-CALL-OVER-COMMITTED"
R_OUTSIDE_WINDOW = "R-CALL-OUTSIDE-WINDOW"
R_WINDOW_PASSED = "R-CALL-WINDOW-PASSED"
R_IDEMPOTENCY_CONFLICT = "R-CALL-IDEMPOTENCY-CONFLICT"
R_RATE_LIMIT = "R-CALL-RATE-LIMIT"
R_FLEET_WIDE = "R-CALL-FLEET-WIDE"
R_NO_OBLIGATION = "R-CALL-OBLIGATION-REQUIRED"
R_ALREADY_ENDED = "R-CALL-ALREADY-ENDED"
R_CANNOT_EXTEND = "R-CALL-CANNOT-EXTEND"

HTTP_NOT_FOUND = 404
HTTP_CONFLICT = 409
HTTP_UNPROCESSABLE = 422
HTTP_TOO_MANY = 429

#: Obligation states a call can deploy (a held award inside its window; SHORTFALL keeps delivering).
DEPLOYABLE_STATES = frozenset({"COMMITTED", "DELIVERING", "SHORTFALL"})
TOLLING_SERVICE_TYPE = "REGULATED_CAPACITY"
TOLLING_VARIANT = "TOLLING"
AS_SERVICE_TYPE = "ERCOT_AS"
#: kW compared with a small tolerance: numeric(12,3) storage rounds to the watt.
KW_TOLERANCE = 1e-3


@dataclass(frozen=True, slots=True)
class Refusal:
    reason_code: str
    detail: str
    http_status: int


@dataclass(frozen=True, slots=True)
class CallLimits:
    """`[dispatch.calls]`: per-principal rate limits and the RAMPING threshold. Operators get the same
    limits as a utility (identical checks); the defaults are far above any real call cadence."""

    max_calls_per_hour: int = 30
    max_calls_per_day: int = 200
    ramping_fraction: float = 0.9

    def __post_init__(self) -> None:
        if self.max_calls_per_hour < 1 or self.max_calls_per_day < self.max_calls_per_hour:
            raise ValueError("[dispatch.calls]: need 1 <= max_calls_per_hour <= max_calls_per_day")
        if not 0.0 < self.ramping_fraction <= 1.0:
            raise ValueError("[dispatch.calls].ramping_fraction must be in (0, 1]")

    @classmethod
    def from_config(cls, cfg: Any) -> CallLimits:
        """`cfg` is duck-typed (`.get(dotted_key, default)`, i.e. `opengrid.platform.config.Config`)."""
        default = cls()
        return cls(
            max_calls_per_hour=int(cfg.get("dispatch.calls.max_calls_per_hour", default.max_calls_per_hour)),
            max_calls_per_day=int(cfg.get("dispatch.calls.max_calls_per_day", default.max_calls_per_day)),
            ramping_fraction=float(cfg.get("dispatch.calls.ramping_fraction", default.ramping_fraction)),
        )


def deployment_kind(service_type: str, variant: str | None) -> CallKind | None:
    """AS for an ERCOT_AS award, UTILITY_CALL for a tolling obligation (D-29), None otherwise."""
    if service_type == AS_SERVICE_TYPE:
        return CallKind.AS
    if service_type == TOLLING_SERVICE_TYPE and (variant or "").upper() == TOLLING_VARIANT:
        return CallKind.UTILITY_CALL
    return None


def call_window(request: CallRequest, now: datetime) -> tuple[datetime, datetime]:
    """The call's `[start, end)`. A start in the past (or none) begins now; the end is `end_at` or the
    requested start plus `duration_minutes`, so a late-arriving instruction keeps its own end."""
    requested_start = request.start_at or now
    if request.end_at is not None:
        end = request.end_at
    else:
        end = requested_start + timedelta(minutes=request.duration_minutes or 0)
    return max(requested_start, now), end


def duration_minutes_of(start: datetime, end: datetime) -> int:
    return max(1, math.ceil((end - start).total_seconds() / 60.0))


def check_request(request: CallRequest, start: datetime, end: datetime, now: datetime) -> Refusal | None:
    """Checks that need no obligation: scope, sign (discharge only), and a window still ahead."""
    if request.fleet_wide:
        return Refusal(
            R_FLEET_WIDE,
            "a fleet-wide deployment needs a two-person approval and is not supported; call one obligation",
            HTTP_CONFLICT,
        )
    if request.requested_kw is not None and request.requested_kw >= 0:
        return Refusal(
            R_CHARGE_REFUSED,
            "a call is discharge only: requested_kw must be negative (+charge/-discharge)",
            HTTP_UNPROCESSABLE,
        )
    if end <= now or end <= start:
        return Refusal(R_WINDOW_PASSED, "the call's end is not in the future", HTTP_CONFLICT)
    return None


def check_award(award: AwardView, request: CallRequest, start: datetime, end: datetime) -> Refusal | None:
    """Checks on the called obligation: kind and origin, state, the product cap (no fallback cap: a
    product without a duration refuses), committed kW, and the reservation window."""
    kind = deployment_kind(award.service_type, award.variant)
    if kind is None or kind not in ALLOWED_KINDS[request.origin]:
        return Refusal(
            R_NOT_DEPLOYABLE,
            f"a {request.origin.value} call cannot deploy this obligation "
            f"({award.service_type}{'/' + award.variant if award.variant else ''})",
            HTTP_CONFLICT,
        )
    if award.state not in DEPLOYABLE_STATES:
        return Refusal(R_STATE, f"obligation is {award.state}, not deployable", HTTP_CONFLICT)
    if not award.duration_minutes:
        return Refusal(
            R_NO_PRODUCT_DURATION,
            "the obligation's product has no deployment duration; cannot cap the call",
            HTTP_CONFLICT,
        )
    return _check_size(award, request, start, end) or _check_window(award, kind, start, end)


def _check_size(award: AwardView, request: CallRequest, start: datetime, end: datetime) -> Refusal | None:
    max_minutes = int(award.duration_minutes or 0)
    if (end - start).total_seconds() > max_minutes * 60 + 1e-6:
        return Refusal(
            R_DURATION_CAP,
            f"duration {duration_minutes_of(start, end)} min exceeds the product limit ({max_minutes} min)",
            HTTP_CONFLICT,
        )
    requested, committed = request.requested_kw, award.committed_kw
    if requested is not None and committed is not None and -requested > committed + KW_TOLERANCE:
        return Refusal(
            R_OVER_COMMITTED,
            f"{-requested:.3f} kW exceeds the committed {committed:.3f} kW",
            HTTP_CONFLICT,
        )
    return None


def _check_window(award: AwardView, kind: CallKind, start: datetime, end: datetime) -> Refusal | None:
    """The call must start inside the obligation's reservation window; a utility call must also end
    inside it (the toll's reservation exists only there). Unknown windows are not checked here."""
    if award.window_start is None or award.window_end is None:
        return None
    if not award.window_start <= start < award.window_end:
        return Refusal(
            R_OUTSIDE_WINDOW,
            f"start {start.isoformat()} is outside the reservation window "
            f"[{award.window_start.isoformat()}, {award.window_end.isoformat()})",
            HTTP_CONFLICT,
        )
    if kind is CallKind.UTILITY_CALL and end > award.window_end:
        return Refusal(
            R_OUTSIDE_WINDOW,
            f"end {end.isoformat()} is after the reservation window end {award.window_end.isoformat()}",
            HTTP_CONFLICT,
        )
    return None


def check_rate(calls_last_hour: int, calls_last_day: int, limits: CallLimits) -> Refusal | None:
    if calls_last_hour >= limits.max_calls_per_hour or calls_last_day >= limits.max_calls_per_day:
        return Refusal(
            R_RATE_LIMIT,
            f"rate limit: at most {limits.max_calls_per_hour} calls/hour and "
            f"{limits.max_calls_per_day} calls/day per principal",
            HTTP_TOO_MANY,
        )
    return None


_STATUS_FOR_REASON: dict[str, int] = {
    R_NOT_FOUND: HTTP_NOT_FOUND,
    R_CHARGE_REFUSED: HTTP_UNPROCESSABLE,
    R_NO_OBLIGATION: HTTP_UNPROCESSABLE,
    R_RATE_LIMIT: HTTP_TOO_MANY,
}


def status_for(reason_code: str | None) -> int:
    """The HTTP status a surface answers for a stored refusal (an idempotent replay of it)."""
    return _STATUS_FOR_REASON.get(reason_code or "", HTTP_CONFLICT)


def overlap_refusal() -> Refusal:
    return Refusal(
        R_OVERLAP,
        "another call already covers part of this window for the obligation (no chaining)",
        HTTP_CONFLICT,
    )


def not_found_refusal() -> Refusal:
    return Refusal(R_NOT_FOUND, "no such obligation", HTTP_NOT_FOUND)


def idempotency_conflict_refusal() -> Refusal:
    return Refusal(
        R_IDEMPOTENCY_CONFLICT,
        "idempotency key already used by this principal for a different call",
        HTTP_CONFLICT,
    )
