"""Personal data must never reach a cloud model (issue #26 hard guardrail; product decision D5).

These are the mandated tests. They assert the *whitelist* behaviour rather than a blocklist: a field
reaches the model only because it was named safe, so a new personal field added upstream tomorrow fails
closed instead of leaking on the next deploy.
"""

from __future__ import annotations

import pytest

from opengrid.ai_agent.redaction import (
    ALLOWED_FIELDS,
    FORBIDDEN_FIELDS,
    contains_personal_data,
    redact,
    redact_text,
)


def test_the_two_lists_can_never_overlap() -> None:
    """If a field were on both lists the allow-list would win somewhere and leak it."""
    assert set() == ALLOWED_FIELDS & FORBIDDEN_FIELDS


def test_redact_drops_every_identifying_field() -> None:
    hub = {
        "hub_id": "hub-00007",
        "bank_id": "bank-000",
        "zone": "LZ_SOUTH",
        "soc_kwh": 21.4,
        "owner": "A Householder",
        "address": "1 Example St, Austin TX",
        "esi_id": "10443720000000000",
        "lat": 30.27,
        "lon": -97.74,
        "telemetry_series": [1, 2, 3],
    }

    cleaned = redact(hub)

    assert cleaned == {"hub_id": "hub-00007", "bank_id": "bank-000", "zone": "LZ_SOUTH", "soc_kwh": 21.4}
    for leaked in ("owner", "address", "esi_id", "lat", "lon", "telemetry_series"):
        assert leaked not in cleaned


def test_the_snapshot_containers_survive_so_the_model_gets_a_state_at_all() -> None:
    """The counterpart to the whitelist: strip the containers too and the model is handed nothing."""
    snapshot = {"health": {"reserve_breaches": 0}, "hubs": {"counts": {"online": 200}}, "obligations": []}

    assert redact(snapshot) == snapshot


def test_an_unknown_field_is_dropped_rather_than_forwarded() -> None:
    """The failure mode that matters: a field nobody has classified yet must not travel."""
    assert redact({"hub_id": "h", "newly_added_personal_thing": "secret"}) == {"hub_id": "h"}


def test_redaction_reaches_inside_lists_and_nested_objects() -> None:
    payload = {"obligations": [{"obligation_id": "o1", "owner": "Someone", "state": "COMMITTED"}]}

    cleaned = redact(payload)

    assert cleaned == {"obligations": [{"obligation_id": "o1", "state": "COMMITTED"}]}


def test_long_free_text_is_truncated() -> None:
    """A long blob is how an address usually sneaks through a structured payload."""
    cleaned = redact({"summary": "x" * 1000})
    assert len(cleaned["summary"]) == 240


def test_contains_personal_data_finds_it_at_any_depth() -> None:
    assert contains_personal_data({"hubs": [{"esi_id": "1044372"}]}) is True
    assert contains_personal_data({"hubs": [{"hub_id": "hub-1"}]}) is False


# --- the question text itself (review item 2) ----------------------------------------------------


@pytest.mark.parametrize(
    ("typed", "category", "leak"),
    [
        ("is the battery at 1200 Barton Springs Rd charging?", "street_address", "Barton"),
        ("what about 4507 Pecan Creek Way, apt 3?", "street_address", "Pecan"),
        ("check 12 Main Street.", "street_address", "Main"),
        ("ESI 10443720000000000 keeps tripping", "esi_id", "10443720000000000"),
        ("ESI 1008901000140010000123 keeps tripping", "esi_id", "1008901000140010000123"),
        ("call the owner on 512-555-0147", "phone", "555"),
        ("call the owner on (512) 555 0147", "phone", "555"),
        ("call the owner on +1 512.555.0147", "phone", "555"),
        ("email jane.doe@example.com about it", "email", "jane.doe"),
        ("the hub at 30.2672, -97.7431 is offline", "lat_lon", "97.7431"),
        ("the hub at 30.267200 -97.743100 is offline", "lat_lon", "97.7431"),
    ],
)
def test_personal_data_typed_into_the_question_is_redacted(typed: str, category: str, leak: str) -> None:
    screened = redact_text(typed)

    assert leak not in screened.text
    assert f"[{category}]" in screened.text
    assert category in screened.found


@pytest.mark.parametrize(
    "ordinary",
    [
        "which obligations are at risk?",
        "why did we decline the ERCOT_AS offers at 5.37 $/MWh?",
        "set bank-007 to 5 kW",
        "is 500 kW on the way for hub-00007?",
        "why was obligation ffcc182c-d1fd-4a2b-9c3e-123456789012 vetoed at 2026-09-26T17:00:00Z?",
        "3 hubs drive the feeder over its limit",
    ],
)
def test_ordinary_operator_questions_are_left_alone(ordinary: str) -> None:
    screened = redact_text(ordinary)

    assert screened.text == ordinary
    assert screened.found == ()


def test_redacting_twice_changes_nothing() -> None:
    once = redact_text("call 512-555-0147 about 1200 Barton Springs Rd").text

    assert redact_text(once).text == once


def test_allow_listed_free_text_fields_are_screened_too() -> None:
    """An alert summary is allow-listed, but its text can still quote an address."""
    cleaned = redact({"alerts": [{"summary": "breaker trip reported at 1200 Barton Springs Rd"}]})

    assert "Barton" not in cleaned["alerts"][0]["summary"]
