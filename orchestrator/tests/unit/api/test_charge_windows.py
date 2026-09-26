"""D-30 owner charge windows on the Fleet page: list, effective (most specific scope wins), two-step
PUT/DELETE with validation, stale-proposal 409, trace old -> new. The responses for the fixed data below are
the UI-FLEET fixtures `fixtures/ui19/charge_windows_*.json` (ids/timestamps normalised)."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from opengrid.api.deps import get_config, get_proposals, get_store, get_trace_store
from opengrid.api.proposals import ProposalStore
from opengrid.api.routers import charge_windows, fleet
from opengrid.platform.config import Config
from opengrid.trace.store import TraceStore

from .conftest import OPERATOR_HEADERS, PROXY_HEADERS, VIEWER_HEADERS
from .fakes import FakeStore, FakeTraceBackend

FIXTURES = Path(__file__).parent / "fixtures" / "ui19"
BASE = "/og/api/fleet/charge-windows"
T0 = datetime(2026, 9, 26, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _provider_tables(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(
        charge_windows,
        "_provider_tables",
        lambda: ({"LZ_AEN": "AUSTIN_ENERGY"}, {"LZ_NORTH": "ONCOR"}),
    )


@pytest.fixture
def store() -> FakeStore:
    fake = FakeStore()
    fake.charge_windows = {
        ("FLEET", "*"): _row("FLEET", "*", ["22:00-06:00"]),
        ("PROVIDER", "ONCOR"): _row("PROVIDER", "ONCOR", ["23:00-05:00"]),
        ("BANK", "bank-007"): _row("BANK", "bank-007", ["01:00-04:00", "13:00-14:00"]),
    }
    fake.topology = {
        ("HUB", "hub-00012"): _topo("hub-00012", "bank-007", "LZ_NORTH"),
        ("HUB", "hub-00013"): _topo("hub-00013", "bank-008", "LZ_NORTH"),
        ("HUB", "hub-00014"): _topo("hub-00014", "bank-009", "LZ_AEN"),
        ("BANK", "bank-007"): _topo(None, "bank-007", "LZ_NORTH"),
    }
    return fake


def _row(kind: str, ref: str, windows: list[str]) -> dict[str, Any]:
    return {"scope_kind": kind, "scope_ref": ref, "windows": windows, "updated_by": "seed", "updated_at": T0}


def _topo(hub_id: str | None, bank_id: str, zone: str) -> dict[str, Any]:
    return {"hub_id": hub_id, "bank_id": bank_id, "zone": zone, "feeder_id": None, "substation_id": None}


@pytest.fixture
def trace() -> TraceStore:
    return TraceStore(FakeTraceBackend())


@pytest.fixture
def api(store: FakeStore, trace: TraceStore) -> TestClient:
    app = FastAPI()
    app.include_router(fleet.router)  # the charge-window routes ride on the fleet router
    proposals = ProposalStore()
    app.dependency_overrides[get_store] = lambda: store
    app.dependency_overrides[get_trace_store] = lambda: trace
    app.dependency_overrides[get_proposals] = lambda: proposals
    app.dependency_overrides[get_config] = lambda: Config({"api": {"roles": {"operator": [], "viewer": []}}})
    return TestClient(app, client=("127.0.0.1", 51234), headers=PROXY_HEADERS)


def _fixture(name: str) -> Any:
    return json.loads((FIXTURES / name).read_text(encoding="utf-8"))


def _normalise(body: dict[str, Any]) -> dict[str, Any]:
    return {k: ("<uuid>" if k in ("proposal_id", "trace_id") else v) for k, v in body.items()}


def test_list(api: TestClient) -> None:
    body = api.get(BASE, headers=VIEWER_HEADERS).json()
    assert body["tz"] == "America/Chicago"
    assert [(i["scope_kind"], i["scope_ref"]) for i in body["items"]] == [
        ("FLEET", "*"),
        ("PROVIDER", "ONCOR"),
        ("BANK", "bank-007"),
    ]
    assert body == _fixture("charge_windows_list.json")


@pytest.mark.parametrize(
    ("hub_id", "source", "windows"),
    [
        ("hub-00012", ("BANK", "bank-007"), ["01:00-04:00", "13:00-14:00"]),  # bank override beats provider
        ("hub-00013", ("PROVIDER", "ONCOR"), ["23:00-05:00"]),  # TDSP of a competitive zone
        ("hub-00014", ("FLEET", "*"), ["22:00-06:00"]),  # AUSTIN_ENERGY has no row: fleet default
    ],
)
def test_effective_resolves_the_most_specific_scope(
    api: TestClient, hub_id: str, source: tuple[str, str], windows: list[str]
) -> None:
    body = api.get(f"{BASE}/effective", headers=VIEWER_HEADERS, params={"hub_id": hub_id}).json()
    assert (body["source"]["scope_kind"], body["source"]["scope_ref"]) == source
    assert body["windows"] == windows
    if hub_id == "hub-00012":
        assert body == _fixture("charge_windows_effective.json")


def test_effective_needs_exactly_one_selector_and_a_known_target(api: TestClient) -> None:
    assert api.get(f"{BASE}/effective", headers=VIEWER_HEADERS).status_code == 422
    params = {"hub_id": "hub-00012", "bank_id": "bank-007"}
    assert api.get(f"{BASE}/effective", headers=VIEWER_HEADERS, params=params).status_code == 422
    assert api.get(f"{BASE}/effective", headers=VIEWER_HEADERS, params={"hub_id": "nope"}).status_code == 404
    bank = api.get(f"{BASE}/effective", headers=VIEWER_HEADERS, params={"bank_id": "bank-007"}).json()
    assert bank["source"] == {"scope_kind": "BANK", "scope_ref": "bank-007"}


def test_put_is_two_step_and_traced_old_to_new(api: TestClient, store: FakeStore, trace: TraceStore) -> None:
    proposed = api.put(
        f"{BASE}/ZONE/LZ_NORTH", headers=OPERATOR_HEADERS, json={"windows": ["21:30-05:30"], "reason": "tou"}
    )
    assert proposed.status_code == 202
    body = proposed.json()
    assert body["old_windows"] is None and body["new_windows"] == ["21:30-05:30"]
    assert ("ZONE", "LZ_NORTH") not in store.charge_windows  # nothing written before confirm
    assert _normalise(body) == _fixture("charge_windows_propose.json")

    confirmed = api.post(f"{BASE}/proposals/{body['proposal_id']}/confirm", headers=OPERATOR_HEADERS)
    assert confirmed.status_code == 200
    assert _normalise(confirmed.json()) == _fixture("charge_windows_confirm.json")
    assert store.charge_windows[("ZONE", "LZ_NORTH")]["windows"] == ["21:30-05:30"]
    assert store.operator_actions[-1]["action_kind"] == "CONFIG_CHANGE"
    records = asyncio.run(trace._backend.fetch_range("operator_action:operator", from_seq=0))  # type: ignore[attr-defined]
    (event,) = [r.payload for r in records if r.event_class == "CHARGE_WINDOW_CHANGE"]
    assert event["old_windows"] is None and event["new_windows"] == ["21:30-05:30"]
    # the new zone row now beats the provider row for hub-00013 (LZ_NORTH)
    eff = api.get(f"{BASE}/effective", headers=VIEWER_HEADERS, params={"hub_id": "hub-00013"}).json()
    assert eff["source"] == {"scope_kind": "ZONE", "scope_ref": "LZ_NORTH"}


@pytest.mark.parametrize(
    "windows",
    [
        ["25:00-01:00"],
        ["10:00-10:00"],
        ["00:00-01:00", "02:00-03:00", "04:00-05:00", "06:00-07:00", "08:00-09:00"],
    ],
)
def test_put_rejects_bad_windows(api: TestClient, windows: list[str]) -> None:
    resp = api.put(
        f"{BASE}/BANK/bank-007", headers=OPERATOR_HEADERS, json={"windows": windows, "reason": "x"}
    )
    assert resp.status_code == 422


def test_put_rejects_bad_scopes(api: TestClient) -> None:
    body = {"windows": ["22:00-06:00"], "reason": "x"}
    assert api.put(f"{BASE}/COUNTY/Travis", headers=OPERATOR_HEADERS, json=body).status_code == 422
    assert api.put(f"{BASE}/FLEET/all", headers=OPERATOR_HEADERS, json=body).status_code == 422
    assert api.put(f"{BASE}/BANK/bank-404", headers=OPERATOR_HEADERS, json=body).status_code == 404
    assert api.put(f"{BASE}/BANK/bank-007", headers=VIEWER_HEADERS, json=body).status_code == 403


def test_an_empty_list_is_a_valid_no_charging_override(api: TestClient, store: FakeStore) -> None:
    proposal = api.put(f"{BASE}/HUB/hub-00012", headers=OPERATOR_HEADERS, json={"windows": [], "reason": "x"})
    api.post(f"{BASE}/proposals/{proposal.json()['proposal_id']}/confirm", headers=OPERATOR_HEADERS)
    eff = api.get(f"{BASE}/effective", headers=VIEWER_HEADERS, params={"hub_id": "hub-00012"}).json()
    assert eff["windows"] == [] and eff["source"] == {"scope_kind": "HUB", "scope_ref": "hub-00012"}


def test_delete_removes_an_override_but_never_the_fleet_default(api: TestClient, store: FakeStore) -> None:
    body = {"reason": "back to provider"}
    assert api.request("DELETE", f"{BASE}/FLEET/*", headers=OPERATOR_HEADERS, json=body).status_code == 409
    assert (
        api.request("DELETE", f"{BASE}/ZONE/LZ_WEST", headers=OPERATOR_HEADERS, json=body).status_code == 404
    )
    proposal = api.request("DELETE", f"{BASE}/BANK/bank-007", headers=OPERATOR_HEADERS, json=body).json()
    assert proposal["new_windows"] is None
    done = api.post(f"{BASE}/proposals/{proposal['proposal_id']}/confirm", headers=OPERATOR_HEADERS).json()
    assert done["windows"] is None and ("BANK", "bank-007") not in store.charge_windows


def test_a_stale_proposal_is_refused(api: TestClient, store: FakeStore) -> None:
    body = {"windows": ["02:00-04:00"], "reason": "x"}
    proposal = api.put(f"{BASE}/BANK/bank-007", headers=OPERATOR_HEADERS, json=body).json()
    store.charge_windows[("BANK", "bank-007")]["windows"] = ["03:00-04:00"]  # someone else changed it
    resp = api.post(f"{BASE}/proposals/{proposal['proposal_id']}/confirm", headers=OPERATOR_HEADERS)
    assert resp.status_code == 409
