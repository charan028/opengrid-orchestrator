"""Fleet R3.1 route helpers: charging-window parsing and grouping, HW/FW URL state, target markers."""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

import pytest
from fastapi.testclient import TestClient

from opengrid.ui.routes.fleet import TableState, charge_window_groups, parse_windows

from .conftest import load_fixture


def test_parse_windows_allows_wrap_and_skips_blank_rows() -> None:
    assert parse_windows(["22:00", "", "13:00"], ["06:00", "", "14:00"]) == ["22:00-06:00", "13:00-14:00"]


@pytest.mark.parametrize(
    ("starts", "ends"),
    [(["22:00"], ["22:00"]), (["25:00"], ["06:00"]), (["1:00"], ["02:00"]), (["01:00"] * 5, ["02:00"] * 5)],
)
def test_parse_windows_rejects_bad_input(starts: list[str], ends: list[str]) -> None:
    with pytest.raises(ValueError, match=r"."):
        parse_windows(starts, ends)


def test_groups_follow_the_scope_hierarchy() -> None:
    body = {
        "tz": "America/Chicago",
        "items": [
            {"scope_kind": "HUB", "scope_ref": "hub-1", "windows": []},
            {"scope_kind": "FLEET", "scope_ref": "*", "windows": ["22:00-06:00"]},
            {"scope_kind": "ZONE", "scope_ref": "LZ_SOUTH", "windows": ["23:00-05:00"]},
        ],
    }
    groups = charge_window_groups(body)
    assert groups is not None and list(groups["groups"]) == ["FLEET", "ZONE", "HUB"]
    assert charge_window_groups(None) is None


def test_hw_fw_state_round_trips_through_the_url() -> None:
    state = TableState(hw=("C1",), fw=("4.2.1",), fw_not="4.3.0")
    assert "hw=C1" in state.url() and "fw_not=4.3.0" in state.url()
    labels = [c["label"] for c in state.chips()]
    assert labels == ["HW: C1", "FW: 4.2.1", "FW \u2260 4.3.0"]


def test_screen_says_charging_schedule_is_coming_without_the_api(
    client: TestClient, fake_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_api({"/og/api/fleet/hubs": load_fixture("hubs.json")})
    body = client.get("/og/fleet", headers={"X-Remote-User": "alice"}).text
    assert 'id="charge-unavailable"' in body
    assert "Firmware update &mdash; available after R3.1" in body


def test_charge_windows_propose_needs_operator_and_valid_windows(
    client: TestClient, fake_post_api: Callable[[dict[str, Any]], None]
) -> None:
    fake_post_api({})
    viewer = client.post("/og/fleet/charge-windows/propose", headers={"X-Remote-User": "carol"}, data={})
    assert viewer.status_code == 403
    bad = client.post(
        "/og/fleet/charge-windows/propose",
        headers={"X-Remote-User": "alice"},
        data={"scope_kind": "BANK", "scope_ref": "bank-1", "start": "22:00", "end": "22:00", "reason": "x"},
    )
    assert "is empty" in bad.text
