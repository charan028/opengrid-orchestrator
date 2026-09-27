"""The r3.4.3 live opcheck failures, reproduced on every answer path (lead / RM, 2026-09-27 03:55).

On prod the routing model's screening timed out for three questions, and the screening-unavailable
path fell back to the snapshot's whole-fleet hub totals ("how many hubs in LZ_NORTH" -> 3509, "how many
hubs are unavailable" -> 3509) or to "assistant unavailable"; and "more than forty kWh" was refused by
the parser but answered through the routing model's own filters. Pinned here, on the no-model path, the
screening-down path and the screened path alike: the same parse gives the same filtered query and the
same answer; a question that names a filter is never answered with the unfiltered total; number words
are numbers; an unread condition is refused whatever the model extracts.
"""

from __future__ import annotations

import re
from typing import Any

import pytest

from opengrid.ai_agent import CopilotService, fleet
from opengrid.ai_agent.budgets import Budget, BudgetLimits
from opengrid.ai_agent.gateway import ModelGateway
from opengrid.ai_agent.types import FleetQuery

from .fakes import CONTEXT, FakeProvider, RecordingTrace
from .test_fleet import _total

#: The prod fleet at 03:55: 3509 hubs, 504 in LZ_NORTH, 1000 on UNAVAILABLE banks, 700 rated 78.4 kWh,
#: 709 rated at least 40 kWh.
FLEET_TOTAL = 3509


class CountingTool:
    """A fleet tool whose count depends on the filter it is given, like the real one."""

    def __init__(self) -> None:
        self.queries: list[FleetQuery] = []

    async def __call__(self, query: FleetQuery) -> dict[str, Any]:
        self.queries.append(query)
        hubs = FLEET_TOTAL
        if query.zones == ("LZ_NORTH",):
            hubs = 504
        elif query.availability == "UNAVAILABLE":
            hubs = 1000
        elif query.capacity_min_kwh == 78.4 and query.capacity_max_kwh == 78.4:
            hubs = 700
        elif query.capacity_min_kwh == 40.0:
            hubs = 709
        return {"total": _total(hubs), "groups": [], "group_by": "none", "rows": []}


def _no_model() -> CopilotService:
    return CopilotService(gateway=ModelGateway(primary=None, budget=Budget(BudgetLimits())))


def _screening_down() -> CopilotService:
    return CopilotService(
        gateway=ModelGateway(primary=FakeProvider(fail=True), budget=Budget(BudgetLimits()))
    )


def _screened(model_filters: dict[str, Any] | None = None) -> CopilotService:
    provider = FakeProvider(intent="fleet_query", fleet=model_filters)
    return CopilotService(gateway=ModelGateway(primary=provider, budget=Budget(BudgetLimits())))


PATHS = {
    "no_model": _no_model,
    "screening_down": _screening_down,
    "screened": _screened,
    # the model's own extraction disagrees; the parser's reading must still decide
    "screened_misleading_model": lambda: _screened({"capacity_min_kwh": 99, "zones": ["LZ_WEST"]}),
}

CASES = [
    ("how many hubs in LZ_NORTH", FleetQuery(zones=("LZ_NORTH",)), "504 hubs in LZ_NORTH."),
    (
        "how many units have capacity 78.4 kWh",
        FleetQuery(capacity_min_kwh=78.4, capacity_max_kwh=78.4),
        "700 hubs rated 78.4 kWh.",
    ),
    (
        "how many hubs are unavailable",
        FleetQuery(availability="UNAVAILABLE"),
        "1,000 hubs on unavailable banks (regulated market, no contract).",
    ),
    (
        "how many hubs are REGULATED_NO_CONTRACT",
        FleetQuery(availability="UNAVAILABLE"),
        "1,000 hubs on unavailable banks",
    ),
    (
        "how many units have more than forty kWh",
        FleetQuery(capacity_min_kwh=40.0),
        "709 hubs rated at least 40 kWh.",
    ),
]


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(("question", "query", "expected"), CASES)
async def test_every_path_answers_the_filtered_count(
    path: str, question: str, query: FleetQuery, expected: str
) -> None:
    tool = CountingTool()

    answer = await PATHS[path]().ask(question, CONTEXT, trace=RecordingTrace(), fleet_tool=tool)

    assert tool.queries == [query], path
    assert answer.text.startswith(expected), (path, answer.text)
    assert answer.tier == "deterministic" and answer.model is None
    first = re.search(r"\d[\d,]*", answer.text)
    assert first is not None and int(first.group(0).replace(",", "")) != FLEET_TOTAL


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "question",
    [
        "how many hubs in LZ_NORTH",
        "how many hubs are unavailable",
        "how many hubs are online?",
    ],
)
async def test_a_filtered_question_without_the_tool_is_never_the_snapshot_total(
    path: str, question: str
) -> None:
    answer = await PATHS[path]().ask(question, CONTEXT, trace=RecordingTrace(), fleet_tool=None)

    assert answer.text == "Can't verify right now: fleet data unavailable.", (path, answer.text)
    assert "200" not in answer.text  # the snapshot's whole-fleet hub count


@pytest.mark.parametrize("path", PATHS)
@pytest.mark.parametrize(
    "question",
    [
        "how many units have more than a lot of kWh",
        "how many hubs in Houston",
        "how many hubs are near the depot",
    ],
)
async def test_an_unread_condition_is_refused_on_every_path(path: str, question: str) -> None:
    tool = CountingTool()
    service = PATHS[path]() if path != "screened" else _screened({"capacity_min_kwh": 40})

    answer = await service.ask(question, CONTEXT, trace=RecordingTrace(), fleet_tool=tool)

    assert answer.refusal_reason == "fleet_condition_unparsed", (path, answer.text)
    assert tool.queries == []


@pytest.mark.parametrize(
    ("words", "digits"),
    [
        ("more than forty kWh", "more than 40 kWh"),
        ("at least seventy-eight kWh", "at least 78 kWh"),
        ("below thirty percent charge", "below 30 percent charge"),
        ("one hundred twenty kW", "120 kW"),
        ("two thousand and five hundred kWh", "2500 kWh"),
    ],
)
def test_number_words_are_read_as_numbers(words: str, digits: str) -> None:
    assert fleet.spell_numbers(words) == digits
