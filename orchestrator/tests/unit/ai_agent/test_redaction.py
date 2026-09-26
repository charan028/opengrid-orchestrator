"""Personal data must never reach a cloud model (issue #26 hard guardrail; product decision D5).

These are the mandated tests. They assert the *whitelist* behaviour rather than a blocklist: a field
reaches the model only because it was named safe, so a new personal field added upstream tomorrow fails
closed instead of leaking on the next deploy.
"""

from __future__ import annotations

from opengrid.ai_agent.redaction import (
    ALLOWED_FIELDS,
    FORBIDDEN_FIELDS,
    contains_personal_data,
    redact,
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
