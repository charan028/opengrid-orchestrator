"""Fleet conditions: comparators are parsed, and an unread condition never becomes a whole-fleet count.

Lead review 2026-09-27 (MEDIUM): "how many units have more than X kWh" parsed to a filterless query, so
the copilot answered with the WHOLE fleet's count. Pinned here: more than / less than / at least /
between on kWh, kW and SoC parse to bounds; a condition the parser cannot read is answered "I couldn't
understand the condition ..." with no count, the fleet tool is not called and the snapshot's hub
totals are not offered instead; the routing model's filters are used only when they cover the
condition.
"""

from __future__ import annotations

import re

import pytest

from opengrid.ai_agent import CopilotService, fleet
from opengrid.ai_agent.budgets import Budget, BudgetLimits
from opengrid.ai_agent.gateway import ModelGateway
from opengrid.ai_agent.types import FleetQuery

from .fakes import CONTEXT, FakeProvider, RecordingTrace
from .test_fleet import FakeFleetTool, _total


def _service(provider: FakeProvider | None = None) -> CopilotService:
    return CopilotService(gateway=ModelGateway(primary=provider, budget=Budget(BudgetLimits())))


@pytest.mark.parametrize(
    ("question", "expected"),
    [
        # rated energy (kWh)
        ("how many hubs have more than 40 kWh", FleetQuery(capacity_min_kwh=40.0)),
        ("how many hubs have less than 40 kWh", FleetQuery(capacity_max_kwh=40.0)),
        ("how many hubs have at least 78.4 kWh", FleetQuery(capacity_min_kwh=78.4)),
        ("how many hubs have at most 39.2 kwh", FleetQuery(capacity_max_kwh=39.2)),
        ("how many hubs between 30 and 50 kWh", FleetQuery(capacity_min_kwh=30.0, capacity_max_kwh=50.0)),
        ("how many hubs have capacity over 40kwh", FleetQuery(capacity_min_kwh=40.0)),
        ("how many hubs have more than 40 kilowatt hours", FleetQuery(capacity_min_kwh=40.0)),
        # rated power (kW)
        ("how many hubs are rated more than 15 kW", FleetQuery(power_min_kw=15.0)),
        ("how many hubs have less than 15 kW", FleetQuery(power_max_kw=15.0)),
        ("how many hubs have at least 11 kilowatts", FleetQuery(power_min_kw=11.0)),
        ("how many hubs between 10 and 20 kW", FleetQuery(power_min_kw=10.0, power_max_kw=20.0)),
        # state of charge (%)
        ("how many hubs are more than 80% charged", FleetQuery(soc_min_pct=80.0)),
        ("how many hubs have less than 20% charge", FleetQuery(soc_max_pct=20.0)),
        ("how many hubs are at least 50 percent charged", FleetQuery(soc_min_pct=50.0)),
        (
            "how many hubs are between 20 and 40 percent charge",
            FleetQuery(soc_min_pct=20.0, soc_max_pct=40.0),
        ),
        ("how many hubs are between 20% and 40% charge", FleetQuery(soc_min_pct=20.0, soc_max_pct=40.0)),
        # combined
        (
            "how many hubs in LZ_NORTH have more than 40 kWh and less than 30% charge",
            FleetQuery(zones=("LZ_NORTH",), capacity_min_kwh=40.0, soc_max_pct=30.0),
        ),
    ],
)
def test_comparators_parse_to_bounds(question: str, expected: FleetQuery) -> None:
    assert fleet.parse(question) == expected


UNPARSEABLE = [
    "how many units have more than X kWh",
    "how many units have more than a lot of kWh",
    "how many hubs have more than 40",
    "how many hubs in LZ_NORTH with low charge",
    "how many hubs have a capacity of about forty",
    "how many hubs are nearly full",
]


@pytest.mark.parametrize("question", UNPARSEABLE)
def test_an_unread_condition_is_flagged_not_dropped(question: str) -> None:
    reading = fleet.parse(question)
    assert isinstance(reading, fleet.UnparsedCondition), reading
    assert reading.text and reading.text in fleet.spell_numbers(question)


def _no_count(text: str, snippet: str) -> bool:
    """No digit in the answer apart from the operator's own quoted words."""
    return re.search(r"\d", text.replace(f"'{snippet}'", "")) is None


@pytest.mark.parametrize("question", UNPARSEABLE)
async def test_an_unread_condition_gets_no_number_without_a_model(question: str) -> None:
    tool = FakeFleetTool({"total": _total(3509), "groups": [], "group_by": "none", "rows": []})

    answer = await _service().ask(question, CONTEXT, trace=RecordingTrace(), fleet_tool=tool)

    reading = fleet.parse(question)
    assert isinstance(reading, fleet.UnparsedCondition)
    assert answer.text.startswith(f"I couldn't understand the condition '{reading.text}'")
    assert _no_count(answer.text, reading.text), answer.text
    assert answer.refusal_reason == "fleet_condition_unparsed"
    assert tool.queries == [], "an unread condition must never run as an unfiltered count"


async def test_the_snapshot_hub_total_is_not_offered_instead() -> None:
    """Without the tool the generic "N hubs are reporting" handler would have answered the fleet total."""
    answer = await _service().ask(
        "how many hubs have more than a lot of kWh", CONTEXT, trace=RecordingTrace()
    )
    assert "200" not in answer.text and answer.refusal_reason == "fleet_condition_unparsed"


async def test_number_words_are_parsed_so_every_path_gives_the_same_count() -> None:
    """r3.4.3 opcheck: "more than forty kWh" was refused by the parser but answered through the routing
    model's filters. Number words are now numbers, so the parser answers it, whatever the model says."""
    claude = FakeProvider(intent="fleet_query", fleet={"capacity_min_kwh": 99})
    tool = FakeFleetTool({"total": _total(709), "groups": [], "group_by": "none", "rows": []})

    answer = await _service(claude).ask(
        "how many units have more than forty kWh", CONTEXT, trace=RecordingTrace(), fleet_tool=tool
    )

    assert tool.queries == [FleetQuery(capacity_min_kwh=40.0)]
    assert answer.text == "709 hubs rated at least 40 kWh." and answer.tier == "deterministic"


async def test_the_routing_models_filter_is_never_trusted_over_an_unread_condition() -> None:
    claude = FakeProvider(intent="fleet_query", fleet={"capacity_min_kwh": 40})
    tool = FakeFleetTool({"total": _total(709), "groups": [], "group_by": "none", "rows": []})

    answer = await _service(claude).ask(
        "how many units have more than a lot of kWh", CONTEXT, trace=RecordingTrace(), fleet_tool=tool
    )

    assert tool.queries == [] and answer.refusal_reason == "fleet_condition_unparsed"


@pytest.mark.parametrize(
    ("question", "model_filters"),
    [
        ("how many hubs in LZ_NORTH with low charge", {"zones": ["LZ_NORTH"]}),  # drops the condition
        ("how many units have more than X kWh", {}),  # nothing extracted
        ("how many units have more than a lot of kWh", {"soc_min_pct": 40}),  # wrong topic
    ],
)
async def test_model_filters_that_miss_the_condition_are_not_run(
    question: str, model_filters: dict[str, object]
) -> None:
    claude = FakeProvider(intent="fleet_query", fleet=model_filters)
    tool = FakeFleetTool({"total": _total(504), "groups": [], "group_by": "none", "rows": []})

    answer = await _service(claude).ask(question, CONTEXT, trace=RecordingTrace(), fleet_tool=tool)

    assert tool.queries == []
    assert answer.refusal_reason == "fleet_condition_unparsed"
    assert answer.screened_by == "fake-screen-1"


async def test_screening_down_still_counts_nothing_for_an_unread_condition() -> None:
    answer = await _service(FakeProvider(fail=True)).ask(
        "how many hubs have more than a lot of kWh",
        CONTEXT,
        trace=RecordingTrace(),
        fleet_tool=FakeFleetTool(),
    )
    assert answer.refusal_reason == "fleet_condition_unparsed" and "200" not in answer.text
