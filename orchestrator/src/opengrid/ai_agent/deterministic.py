"""Tier one: answers that need no model at all (issue #26 item 1). Owner: ui-a/ai.

Most of what an operator asks a copilot is a lookup the console already performed to draw the screen.
Answering those in code is not a shortcut around the AI -- it is the requirement: UI-GLB-08 says that
with AI switched off "every Why? panel and explanation shows the deterministic template text, unchanged
in substance". This module *is* that text, so AI-off parity is structural rather than a second
implementation someone has to remember to keep in step.

Every handler returns citations. An answer with nothing behind it is not returned at all.
"""

from __future__ import annotations

from typing import Any

from opengrid.ai_agent.types import Citation, CopilotAnswer

_AT_RISK = ("at risk", "at_risk", "in trouble", "jeopardy")
_DECLINED = ("declin", "reject", "not select", "why not", "passed on", "skip")
_COMMITTED = ("committed", "promised", "selling", "delivering", "obligation")
_FLEET = ("hub", "fleet", "battery", "offline", "fault", "online", "stale")
_PROMISES = ("breach", "sold twice", "double", "promise", "invariant", "reserve")
_ALERTS = ("alert", "alarm", "warning", "wrong", "problem")


def _matches(question: str, needles: tuple[str, ...]) -> bool:
    lowered = question.lower()
    return any(needle in lowered for needle in needles)


def _number(value: Any, default: float = 0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default


#: The snapshot key that lists the sources whose read failed (set by the API router's `snapshot`).
UNAVAILABLE_KEY = "unavailable"

#: What each source is called in a "can't verify" answer, and where the operator would look for it.
_SOURCES: dict[str, tuple[str, str]] = {
    "obligations": ("obligation data", "/og/api/dispatch/opportunities"),
    "invariants": ("invariant data", "/og/api/health"),
    "alerts": ("alert data", "/og/api/health"),
    "hubs": ("hub health data", "/og/api/health"),
}

_INVARIANT_FIELDS = ("reserve_breaches", "double_sold_kwh")


def unavailable_sources(context: dict[str, Any]) -> frozenset[str]:
    """Which sources this snapshot cannot vouch for: those the router flagged as failed, plus any whose
    data is simply absent. Absence is never read as "nothing to report"."""
    flagged = {str(name) for name in (context.get(UNAVAILABLE_KEY) or [])}
    health = context.get("health")
    hubs = context.get("hubs")
    if "health" in flagged:
        flagged |= {"invariants", "alerts", "hubs"}
    if "obligations" not in context:
        flagged.add("obligations")
    if not isinstance(health, dict) or any(field not in health for field in _INVARIANT_FIELDS):
        flagged.add("invariants")
    if not isinstance(health, dict) or "alerts" not in health:
        flagged.add("alerts")
    if not isinstance(hubs, dict) or not hubs.get("counts"):
        flagged.add("hubs")
    return frozenset(flagged & set(_SOURCES))


def _cannot_verify(source: str) -> CopilotAnswer:
    """The honest answer when the data behind a question could not be read: never a default "OK"."""
    what, path = _SOURCES[source]
    return CopilotAnswer(
        text=f"Can't verify right now: {what} unavailable.",
        tier="deterministic",
        refusal_reason=f"{source}_unavailable",
        citations=[Citation(source=path, ref=f"{source}_unavailable", label=f"{what} unavailable")],
    )


def answer(question: str, context: dict[str, Any]) -> CopilotAnswer | None:
    """The deterministic answer to `question`, or None when no handler applies.

    `context` is the read-only snapshot the API router assembled: `health`, `obligations`, `hubs`, and
    `unavailable` (the sources whose read failed). A handler whose source is unavailable answers "can't
    verify right now", never the value an empty source would imply.
    """
    obligations: list[dict[str, Any]] = list(context.get("obligations") or [])
    health: dict[str, Any] = dict(context.get("health") or {})
    hubs: dict[str, Any] = dict(context.get("hubs") or {})
    missing = unavailable_sources(context)

    if _matches(question, _AT_RISK):
        if "obligations" in missing:
            return _cannot_verify("obligations")
        flagged = [o for o in obligations if o.get("at_risk")]
        if not flagged:
            return CopilotAnswer(
                text="No obligation is flagged at risk right now.",
                tier="deterministic",
                citations=[
                    Citation(source="/og/api/dispatch/opportunities", ref="all", label="obligation list")
                ],
            )
        lines = ", ".join(
            f"{o.get('obligation_id', '?')[:8]} ({o.get('service_type', 'unknown')}"
            + (f", {o['last_reason_code']}" if o.get("last_reason_code") else "")
            + ")"
            for o in flagged[:8]
        )
        return CopilotAnswer(
            text=f"{len(flagged)} obligation{'' if len(flagged) == 1 else 's'} flagged at risk: {lines}.",
            tier="deterministic",
            citations=[
                Citation(
                    source="/og/api/dispatch/opportunities",
                    ref=str(o.get("obligation_id", "?")),
                    label=f"{o.get('service_type', 'obligation')} {str(o.get('obligation_id', ''))[:8]}",
                )
                for o in flagged[:8]
            ],
        )

    if _matches(question, _DECLINED):
        if "obligations" in missing:
            return _cannot_verify("obligations")
        offered = [o for o in obligations if o.get("state") == "OFFERED"]
        priced = [o for o in offered if o.get("value_per_mwh") is not None]
        under = [
            o for o in priced if _number(o.get("value_per_mwh")) < _number(o.get("degradation_cost")) * 1000.0
        ]
        if under:
            sample = under[0]
            value = _number(sample.get("value_per_mwh"))
            degradation = _number(sample.get("degradation_cost")) * 1000.0
            return CopilotAnswer(
                text=(
                    f"{len(under)} of {len(offered)} offers on the board price below what they cost to "
                    f"serve, so the optimizer left them. For example "
                    f"{str(sample.get('obligation_id', ''))[:8]} pays {value:.2f} $/MWh against "
                    f"{degradation:.0f} $/MWh of battery degradation."
                ),
                tier="deterministic",
                citations=[
                    Citation(
                        source="/og/api/dispatch/opportunities",
                        ref=str(o.get("obligation_id", "?")),
                        label=f"{o.get('service_type', 'offer')} at {_number(o.get('value_per_mwh')):.2f} $/MWh",
                    )
                    for o in under[:6]
                ],
            )
        if offered:
            return CopilotAnswer(
                text=(
                    f"{len(offered)} offer{'' if len(offered) == 1 else 's'} are still open and none of "
                    "them price below their degradation cost, so they are waiting for the next "
                    "quarter-hour gate rather than being declined."
                ),
                tier="deterministic",
                citations=[
                    Citation(source="/og/api/dispatch/opportunities", ref="OFFERED", label="open offers")
                ],
            )

    if _matches(question, _PROMISES):
        if "invariants" in missing:
            return _cannot_verify("invariants")
        breaches = int(_number(health.get("reserve_breaches")))
        double_sold = int(_number(health.get("double_sold_kwh")))
        # Commitment switches have no measuring check yet; the health route reports a constant for them,
        # which the snapshot leaves out. Unmeasured is said as such, and never counted as "held".
        switches_measured = "commitment_switches" in health
        switches = int(_number(health.get("commitment_switches"))) if switches_measured else 0
        broken = breaches + double_sold + switches
        if broken:
            state = f"{broken} promise{'' if broken == 1 else 's'} broke today."
        elif switches_measured:
            state = "Every promise has held today."
        else:
            state = "No reserve breach or double sale is recorded today."
        switch_text = (
            f"commitment switches {switches}" if switches_measured else "commitment switches not measured"
        )
        return CopilotAnswer(
            text=(
                f"{state} Reserve breaches {breaches}, kWh sold twice {double_sold}, {switch_text}. "
                "Each measured counter is checked continuously and written to the trace."
            ),
            tier="deterministic",
            citations=[Citation(source="/og/api/health", ref="invariants", label="live health counters")],
        )

    if _matches(question, _COMMITTED):
        if "obligations" in missing:
            return _cannot_verify("obligations")
        committed = [o for o in obligations if o.get("state") in ("COMMITTED", "DELIVERING")]
        total_kw = sum(_number(o.get("committed_qty_kw")) for o in committed)
        if committed:
            return CopilotAnswer(
                text=(
                    f"{len(committed)} obligation{'' if len(committed) == 1 else 's'} are committed or "
                    f"delivering, {total_kw:.0f} kW in total."
                ),
                tier="deterministic",
                citations=[
                    Citation(
                        source="/og/api/dispatch/opportunities",
                        ref=str(o.get("obligation_id", "?")),
                        label=f"{o.get('service_type', 'obligation')} {_number(o.get('committed_qty_kw')):.0f} kW",
                    )
                    for o in committed[:8]
                ],
            )
        return CopilotAnswer(
            text="Nothing is committed right now; the board shows only open offers.",
            tier="deterministic",
            citations=[Citation(source="/og/api/dispatch/opportunities", ref="all", label="obligation list")],
        )

    if _matches(question, _FLEET):
        if "hubs" in missing:
            return _cannot_verify("hubs")
        counts = {k: int(_number(v)) for k, v in (hubs.get("counts") or {}).items()}
        total = sum(counts.values())
        if total:
            detail = ", ".join(f"{name} {count}" for name, count in counts.items() if count)
            return CopilotAnswer(
                text=f"{total} hubs are reporting: {detail}.",
                tier="deterministic",
                citations=[Citation(source="/og/api/health", ref="hub_health_counts", label="hub health")],
            )

    if _matches(question, _ALERTS):
        if "alerts" in missing:
            return _cannot_verify("alerts")
        alerts = list(health.get("alerts") or [])
        if not alerts:
            return CopilotAnswer(
                text="There are no open alerts.",
                tier="deterministic",
                citations=[Citation(source="/og/api/health", ref="alerts", label="open alerts")],
            )
        lines = "; ".join(f"{a.get('severity', '?')}: {a.get('summary', '')}" for a in alerts[:5])
        return CopilotAnswer(
            text=f"{len(alerts)} open alert{'' if len(alerts) == 1 else 's'}. {lines}.",
            tier="deterministic",
            citations=[
                Citation(
                    source="/og/api/health", ref=str(a.get("id", "?")), label=str(a.get("summary", "alert"))
                )
                for a in alerts[:5]
            ],
        )

    return None
