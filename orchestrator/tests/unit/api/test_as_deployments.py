"""Operator-triggered ERCOT_AS deployment (the demo's stand-in for an ERCOT deployment instruction): an AS
award is a 0 kW capacity hold until deployed; `POST /og/api/dispatch/as-deployments` deploys ONE award.

Review finding (2026-09-26): the award must exist and be a deployable ERCOT_AS award (404/409, never a
500 from the foreign key), the duration is capped by its product (ECRS 60 min, Non-Spin 240), and a
fleet-wide deployment is refused (it would need a two-person approval)."""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest

from .conftest import OPERATOR_HEADERS, VIEWER_HEADERS

URL = "/og/api/dispatch/as-deployments"


def _award(fake_store, *, service_type="ERCOT_AS", state="DELIVERING", duration_minutes=60) -> UUID:
    obligation_id = uuid4()
    fake_store.as_awards[obligation_id] = {
        "service_type": service_type,
        "state": state,
        "duration_minutes": duration_minutes,
    }
    return obligation_id


def test_an_operator_deploys_one_award_and_it_is_recorded(client, fake_store) -> None:
    obligation_id = _award(fake_store)
    resp = client.post(
        URL,
        headers=OPERATOR_HEADERS,
        json={"obligation_id": str(obligation_id), "duration_minutes": 10, "reason": "ERCOT deploy"},
    )

    assert resp.status_code == 201
    body = resp.json()
    assert body["obligation_id"] == str(obligation_id)
    (deployment,) = fake_store.as_deployments.values()
    assert deployment["requested_by"] == "operator"
    (action,) = fake_store.operator_actions
    assert action["target_ref"] == f"AS_DEPLOYMENT:{obligation_id}"
    assert action["trace_id"] is not None  # traced before it takes effect (K10)

    listed = client.get(URL, headers=VIEWER_HEADERS).json()
    assert [d["deployment_id"] for d in listed] == [body["deployment_id"]]


def test_a_deployment_can_be_ended_early(client, fake_store) -> None:
    obligation_id = _award(fake_store)
    body = client.post(
        URL, headers=OPERATOR_HEADERS, json={"obligation_id": str(obligation_id), "reason": "partial deploy"}
    ).json()

    assert client.delete(f"{URL}/{body['deployment_id']}", headers=OPERATOR_HEADERS).status_code == 200
    assert client.get(URL, headers=VIEWER_HEADERS).json() == []
    assert client.delete(f"{URL}/{body['deployment_id']}", headers=OPERATOR_HEADERS).status_code == 404


def test_an_unknown_obligation_is_404_not_a_database_error(client, fake_store) -> None:
    resp = client.post(URL, headers=OPERATOR_HEADERS, json={"obligation_id": str(uuid4()), "reason": "x"})
    assert resp.status_code == 404
    assert fake_store.as_deployments == {}


@pytest.mark.parametrize(
    ("service_type", "state"),
    [("PARTNER_CAPACITY", "DELIVERING"), ("ERCOT_AS", "SETTLED"), ("ERCOT_AS", "OFFERED")],
)
def test_a_non_as_or_non_deployable_award_is_409(client, fake_store, service_type, state) -> None:
    obligation_id = _award(fake_store, service_type=service_type, state=state)
    resp = client.post(
        URL, headers=OPERATOR_HEADERS, json={"obligation_id": str(obligation_id), "reason": "x"}
    )
    assert resp.status_code == 409
    assert fake_store.as_deployments == {}


@pytest.mark.parametrize(
    ("product_minutes", "requested", "code"), [(60, 60, 201), (60, 61, 409), (240, 240, 201)]
)
def test_duration_is_capped_by_the_awards_product(
    client, fake_store, product_minutes, requested, code
) -> None:
    obligation_id = _award(fake_store, duration_minutes=product_minutes)
    resp = client.post(
        URL,
        headers=OPERATOR_HEADERS,
        json={"obligation_id": str(obligation_id), "duration_minutes": requested, "reason": "x"},
    )
    assert resp.status_code == code


def test_a_fleet_wide_deployment_is_refused(client, fake_store) -> None:
    _award(fake_store)
    assert client.post(URL, headers=OPERATOR_HEADERS, json={"scope": "ALL", "reason": "x"}).status_code == 409
    assert client.post(URL, headers=OPERATOR_HEADERS, json={"reason": "x"}).status_code == 422
    assert fake_store.as_deployments == {}


def test_a_viewer_cannot_deploy_and_duration_is_bounded(client, fake_store) -> None:
    obligation_id = str(_award(fake_store, duration_minutes=240))
    assert (
        client.post(
            URL, headers=VIEWER_HEADERS, json={"obligation_id": obligation_id, "reason": "x"}
        ).status_code
        == 403
    )
    too_long = client.post(
        URL,
        headers=OPERATOR_HEADERS,
        json={"obligation_id": obligation_id, "duration_minutes": 241, "reason": "x"},
    )
    assert too_long.status_code == 422
