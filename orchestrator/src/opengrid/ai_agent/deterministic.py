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


def answer(question: str, context: dict[str, Any]) -> CopilotAnswer | None:
    """The deterministic answer to `question`, or None when no handler applies.

    `context` is the already-redacted read-only snapshot the API router assembled: `health`,
    `obligations` and `hubs`.
    """
    obligations: list[dict[str, Any]] = list(context.get("obligations") or [])
    health: dict[str, Any] = dict(context.get("health") or {})
    hubs: dict[str, Any] = dict(context.get("hubs") or {})

    if _matches(question, _AT_RISK):
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
        breaches = int(_number(health.get("reserve_breaches")))
        double_sold = int(_number(health.get("double_sold_kwh")))
        switches = int(_number(health.get("commitment_switches")))
        broken = breaches + double_sold + switches
        state = (
            "Every promise has held today."
            if broken == 0
            else f"{broken} promise{'' if broken == 1 else 's'} broke today."
        )
        return CopilotAnswer(
            text=(
                f"{state} Reserve breaches {breaches}, kWh sold twice {double_sold}, commitment "
                f"switches {switches}. Each is counted every two-second cycle and written to the trace."
            ),
            tier="deterministic",
            citations=[Citation(source="/og/api/health", ref="invariants", label="live health counters")],
        )

    if _matches(question, _COMMITTED):
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
