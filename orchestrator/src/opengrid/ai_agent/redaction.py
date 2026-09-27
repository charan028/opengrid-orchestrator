"""Personal data never reaches a cloud model. Owner: ui-a/ai.

The UI spec is explicit that this is *our* decision, not the model's: "The console\'s own request layer,
not the model\'s discretion, decides this" (S3.0(h)), and the product\'s binding decision D5 forbids
sharing the console\'s personal data with a third party or a cloud LLM.

So this module is a whitelist, not a blocklist. A field reaches the model only if it is named here as
non-personal. Anything unrecognised is dropped, which means a new personal field added upstream fails
closed instead of leaking on the next deploy.

Free text is the other way in: an operator can type an address, an ESI ID, a phone number, an email or
a coordinate pair straight into the question. `redact_text` screens the question itself and replaces
each of those with a placeholder before the text reaches any model or the trace.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
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
        # The fleet tool's result (counts and totals over hubs; per-hub rows carry equipment ids, zone,
        # ratings, SoC and health only -- the tool never returns a position).
        "fleet",
        "groups",
        "rows",
        "mobile",
        # Values.
        "asset_class",
        "at_home",
        "at_home_ids",
        "available_hubs",
        "available_kw",
        "available_kwh",
        "away",
        "away_ids",
        "e_kwh",
        "group_by",
        "key",
        "rated_kw",
        "rated_kwh",
        "rated_p_kw",
        "units",
        "unknown",
        "activity",
        "age_s",
        "active_commitments",
        "at_risk",
        "available",
        "commitment_switches",
        "lock_violations",
        "offline",
        "open_alert_count",
        "stale",
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
        "unavailable",
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
    if isinstance(value, bool | int | float) or value is None:
        return value
    # Allow-listed string fields are free text too (an alert summary can quote an address), so they are
    # screened like the question before truncation.
    return redact_text(str(value)).text[:_MAX_STRING]


#: Street suffixes that are not ordinary English words, so they are matched in any case.
_STREET_SUFFIX_ANY_CASE = (
    r"street|st|avenue|ave|road|rd|boulevard|blvd|dr|ln|ct|parkway|pkwy|highway|hwy|cir|trl|cv"
)
#: Suffixes that are also everyday words ("500 kW on the way"): matched only when written as a proper
#: name, i.e. capitalised together with the street name before them ("12 Pecan Way").
_STREET_SUFFIX_PROPER = r"Way|Place|Court|Drive|Lane|Trail|Loop|Circle|Cove|Run|Path|Pass|Row|Square|Terrace"
_UNIT = r"(?:\s*(?:apt|unit|suite|ste|#)\s*[\w-]+)?"

#: Order matters: the long digit runs (ESI IDs) and coordinate pairs are matched before the phone
#: pattern, which would otherwise claim part of them.
_TEXT_PATTERNS: tuple[tuple[str, re.Pattern[str]], ...] = (
    ("email", re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")),
    # ERCOT ESI IDs are 17 or 22 digits (the 22-digit form carries a TDSP prefix).
    ("esi_id", re.compile(r"(?<!\d)(?:\d{22}|\d{17})(?!\d)")),
    # Coordinates carry at least three decimals; prices and percentages ("5.37, 30.00") do not.
    (
        "lat_lon",
        re.compile(r"(?<![\d.])[-+]?\d{1,2}\.\d{3,}\s*[,;/ ]\s*[-+]?\d{1,3}\.\d{3,}(?![\d.])"),
    ),
    (
        "phone",
        re.compile(r"(?<![\w])(?:\+?1[\s.-]?)?(?:\(\d{3}\)|\d{3})[\s.-]?\d{3}[\s.-]?\d{4}(?!\d)"),
    ),
    (
        "street_address",
        re.compile(
            rf"\b\d{{1,6}}(?:\s+[A-Za-z0-9][A-Za-z0-9.'-]*){{1,4}}?\s+(?:{_STREET_SUFFIX_ANY_CASE})\b\.?{_UNIT}",
            re.IGNORECASE,
        ),
    ),
    (
        "street_address",
        re.compile(
            rf"\b\d{{1,6}}(?:\s+[A-Z0-9][A-Za-z0-9.'-]*){{1,4}}?\s+(?:{_STREET_SUFFIX_PROPER})\b{_UNIT}"
        ),
    ),
)


@dataclass(frozen=True, slots=True)
class RedactedText:
    """Operator text after screening: `text` is safe to send and to trace; `found` names what was
    removed (categories only, never the values)."""

    text: str
    found: tuple[str, ...] = ()


def redact_text(text: str) -> RedactedText:
    """Replace personal data typed into free text with `[category]` placeholders. Idempotent."""
    found: list[str] = []
    for category, pattern in _TEXT_PATTERNS:
        text, count = pattern.subn(f"[{category}]", text)
        if count and category not in found:
            found.append(category)
    return RedactedText(text=text, found=tuple(found))


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
