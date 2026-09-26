"""No false assurance: a deterministic answer whose source could not be read says so (lead's R3 fix).

"Every promise held", "no open alerts", "nothing at risk" are claims about data. When that data is
unavailable the copilot answers "can't verify right now: <source> unavailable" and cites the gap; it
never falls back to the value an empty source would imply.
"""

from __future__ import annotations

import copy
from typing import Any

import pytest

from opengrid.ai_agent import deterministic
from opengrid.api.routers import ai as ai_routes

from ..api.fakes import FakeStore
from .fakes import CONTEXT, FakeProvider, RecordingTrace


def _without(**flags: Any) -> dict[str, Any]:
    context = copy.deepcopy(CONTEXT)
    context.update(flags)
    return context


@pytest.mark.parametrize(
    ("question", "unavailable", "what", "source"),
    [
        ("were any promises broken?", ["invariants"], "invariant data", "/og/api/health"),
        ("any reserve breaches today?", ["health"], "invariant data", "/og/api/health"),
        (
            "which obligations are at risk?",
            ["obligations"],
            "obligation data",
            "/og/api/dispatch/opportunities",
        ),
        (
            "why did we decline the offers?",
            ["obligations"],
            "obligation data",
            "/og/api/dispatch/opportunities",
        ),
        (
            "what is committed right now?",
            ["obligations"],
            "obligation data",
            "/og/api/dispatch/opportunities",
        ),
        ("any open alerts?", ["health"], "alert data", "/og/api/health"),
        ("how is the fleet?", ["health"], "hub health data", "/og/api/health"),
    ],
)
def test_an_unavailable_source_is_never_reported_as_ok(
    question: str, unavailable: list[str], what: str, source: str
) -> None:
    answer = deterministic.answer(question, _without(unavailable=unavailable))

    assert answer is not None
    assert answer.text == f"Can't verify right now: {what} unavailable."
    assert answer.citations[0].source == source
    assert answer.citations[0].ref.endswith("_unavailable")
    for assurance in (
        "held",
        "No obligation",
        "no open alerts",
        "Nothing is committed",
        "hubs are reporting",
    ):
        assert assurance not in answer.text


def test_missing_invariant_counters_are_unknown_not_zero() -> None:
    """Absence is not "nothing to report": no counters in the snapshot means no promise answer."""
    context = _without()
    del context["health"]["reserve_breaches"]

    answer = deterministic.answer("were any promises broken?", context)

    assert answer is not None and "Can't verify right now: invariant data unavailable" in answer.text


def test_measured_counters_still_answer_normally() -> None:
    answer = deterministic.answer("were any promises broken?", CONTEXT)

    assert answer is not None and answer.text.startswith("Every promise has held today.")


def test_unmeasured_commitment_switches_are_not_claimed_as_held() -> None:
    context = _without()
    del context["health"]["commitment_switches"]

    answer = deterministic.answer("were any promises broken?", context)

    assert answer is not None
    assert "Every promise has held" not in answer.text
    assert "commitment switches not measured" in answer.text


async def test_the_service_passes_the_unknown_through_unchanged() -> None:
    from opengrid.ai_agent import CopilotService
    from opengrid.ai_agent.budgets import Budget, BudgetLimits
    from opengrid.ai_agent.gateway import ModelGateway

    service = CopilotService(gateway=ModelGateway(primary=FakeProvider(), budget=Budget(BudgetLimits())))

    answer = await service.ask(
        "were any promises broken?", _without(unavailable=["invariants"]), trace=RecordingTrace()
    )

    assert answer.text == "Can't verify right now: invariant data unavailable."
    assert answer.refusal_reason == "invariants_unavailable"


# --- the router's snapshot marks what it could not read ------------------------------------------


class _FailingStore:
    def __init__(self, *failing: str) -> None:
        self._inner = FakeStore()
        self._failing = set(failing)

    def __getattr__(self, name: str) -> Any:
        if name in self._failing:

            async def _boom(*_args: Any, **_kwargs: Any) -> Any:
                raise ConnectionError("db down")

            return _boom
        return getattr(self._inner, name)


async def test_a_failed_health_read_is_marked_unavailable() -> None:
    view = await ai_routes.snapshot(_FailingStore("health_snapshot"), None)  # type: ignore[arg-type]

    assert "health" in view["unavailable"]
    answer = deterministic.answer("any open alerts?", view)
    assert answer is not None and answer.text == "Can't verify right now: alert data unavailable."


async def test_a_failed_obligation_read_is_marked_unavailable() -> None:
    view = await ai_routes.snapshot(_FailingStore("list_obligations"), None)  # type: ignore[arg-type]

    assert "obligations" in view["unavailable"] and "obligations" not in view
    answer = deterministic.answer("which obligations are at risk?", view)
    assert answer is not None and answer.text == "Can't verify right now: obligation data unavailable."


async def test_unmeasured_invariants_are_marked_unavailable() -> None:
    """With no invariant read (here: no pool) the health route reports zeros it never measured."""
    view = await ai_routes.snapshot(FakeStore(), None)  # type: ignore[arg-type]

    assert "invariants" in view["unavailable"]
    assert "reserve_breaches" not in view["health"]
    answer = deterministic.answer("were any promises broken?", view)
    assert answer is not None and answer.text == "Can't verify right now: invariant data unavailable."
