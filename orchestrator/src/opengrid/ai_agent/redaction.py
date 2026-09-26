"""Personal data never reaches a cloud model. Owner: ui-a/ai.

The UI spec is explicit that this is *our* decision, not the model's: "The console\'s own request layer,
not the model\'s discretion, decides this" (S3.0(h)), and the product\'s binding decision D5 forbids
sharing the console\'s personal data with a third party or a cloud LLM.

So this module is a whitelist, not a blocklist. A field reaches the model only if it is named here as
non-personal. Anything unrecognised is dropped, which means a new personal field added upstream fails
closed instead of leaking on the next deploy.
"""

from __future__ import annotations

from typing import Any

#: Fields that are safe to send: identifiers of equipment and contracts, states, and aggregate numbers.
#: Deliberately excludes anything that identifies a household or locates one -- name, address, ESI ID,
#: meter id, lat/lon, and any per-premise time series (S3.0(j): household routines are inferable from
#: those even without a name attached).
ALLOWED_FIELDS: frozenset[str] = frozenset(
    {
        # Structural containers of the snapshot itself. They carry no value of their own, but without
        # them the whole context is stripped and the model is handed an empty state (caught by test).
        "alerts",
        "banks",
        "context",
        "counts",
        "health",
        "hubs",
        "items",
        "obligations",
        "zones",
        # Values.
        "activity",
        "age_s",
        "at_risk",
        "available",
        "awarded",
        "bank_id",
        "committed_qty_kw",
        "confidence",
        "count",
        "degradation_cost",
        "double_sold_kwh",
        "event_class",
        "feeder_id",
        "fleet_mw",
        "gate_kind",
        "hub_id",
        "id",
        "interval_end",
        "interval_start",
        "kva_rating",
        "kw",
        "last_reason_code",
        "obligation_id",
        "objective_value",
        "online",
        "opened_at",
        "opportunity_id",
        "outcome",
        "p_kw",
        "plan_id",
        "plan_mode",
        "product",
        "reason_code",
        "reason_codes",
        "rejected",
        "reserve_breaches",
        "service_type",
        "severity",
        "soc_kwh",
        "soc_pct",
        "solver_status",
        "state",
        "submitted",
        "summary",
        "tier",
        "total",
        "trace_id",
        "value_per_mwh",
        "window_end",
        "window_start",
        "zone",
    }
)

#: Names that must never be sent even if some future refactor adds them to the allow-list by accident.
#: Belt and braces: `redact` checks this first, and a test asserts the two sets never overlap.
FORBIDDEN_FIELDS: frozenset[str] = frozenset(
    {
        "address",
        "customer_name",
        "email",
        "esi_id",
        "household",
        "lat",
        "lon",
        "meter_id",
        "occupant",
        "owner",
        "phone",
        "postcode",
        "premise_id",
        "service_address",
        "telemetry_series",
        "zip",
    }
)

_MAX_STRING = 240


class PersonalDataError(Exception):
    """Raised when a caller hands the agent something that cannot be made safe to send."""


def redact(value: Any, *, _depth: int = 0) -> Any:
    """Return `value` with every non-allow-listed field removed, ready to send to a model.

    Dicts are filtered by key; lists are filtered element-wise; scalars pass through with long strings
    truncated (a long free-text blob is the usual way an address sneaks through a structured payload).
    Nesting is capped so a pathological payload cannot blow the request up.
    """
    if _depth > 6:
        return None
    if isinstance(value, dict):
        out: dict[str, Any] = {}
        for key, item in value.items():
            name = str(key)
            if name in FORBIDDEN_FIELDS or name not in ALLOWED_FIELDS:
                continue
            cleaned = redact(item, _depth=_depth + 1)
            if cleaned is not None:
                out[name] = cleaned
        return out
    if isinstance(value, list):
        return [redact(item, _depth=_depth + 1) for item in value[:50]]
    if isinstance(value, str):
        return value[:_MAX_STRING]
    if isinstance(value, bool | int | float) or value is None:
        return value
    return str(value)[:_MAX_STRING]


def contains_personal_data(value: Any) -> bool:
    """True if `value` carries any field this module refuses to send. Used to decline a request loudly
    rather than quietly shipping a stripped version the operator did not expect."""
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key) in FORBIDDEN_FIELDS or contains_personal_data(item):
                return True
        return False
    if isinstance(value, list):
        return any(contains_personal_data(item) for item in value)
    return False
