"""Operator-triggered ERCOT_AS deployment (the demo's stand-in for an ERCOT deployment instruction): an AS
award is a 0 kW capacity hold until deployed; `POST /og/api/dispatch/as-deployments` deploys it."""

from __future__ import annotations

from uuid import uuid4

from .conftest import OPERATOR_HEADERS, VIEWER_HEADERS

URL = "/og/api/dispatch/as-deployments"


def test_an_operator_deploys_every_held_award_and_it_is_recorded(client, fake_store) -> None:
    resp = client.post(URL, headers=OPERATOR_HEADERS, json={"duration_minutes": 10, "reason": "ERCOT deploy"})

    assert resp.status_code == 201
    body = resp.json()
    assert body["obligation_id"] is None  # all ERCOT_AS awards
    (deployment,) = fake_store.as_deployments.values()
    assert deployment["requested_by"] == "operator"
    (action,) = fake_store.operator_actions
    assert action["target_ref"] == "AS_DEPLOYMENT:ERCOT_AS:ALL"
    assert action["trace_id"] is not None  # traced before it takes effect (K10)

    listed = client.get(URL, headers=VIEWER_HEADERS).json()
    assert [d["deployment_id"] for d in listed] == [body["deployment_id"]]


def test_one_obligation_can_be_deployed_and_ended_early(client, fake_store) -> None:
    obligation_id = str(uuid4())
    body = client.post(
        URL, headers=OPERATOR_HEADERS, json={"obligation_id": obligation_id, "reason": "partial deploy"}
    ).json()
    assert body["obligation_id"] == obligation_id

    assert client.delete(f"{URL}/{body['deployment_id']}", headers=OPERATOR_HEADERS).status_code == 200
    assert client.get(URL, headers=VIEWER_HEADERS).json() == []
    assert client.delete(f"{URL}/{body['deployment_id']}", headers=OPERATOR_HEADERS).status_code == 404


def test_a_viewer_cannot_deploy_and_duration_is_bounded(client) -> None:
    assert client.post(URL, headers=VIEWER_HEADERS, json={"reason": "x"}).status_code == 403
    too_long = client.post(URL, headers=OPERATOR_HEADERS, json={"duration_minutes": 241, "reason": "x"})
    assert too_long.status_code == 422
