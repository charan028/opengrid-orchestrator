"""R2: validation of an operator AS deployment (`POST /og/api/dispatch/as-deployments`), test plan TS-05-16.

A deployment is the demo's stand-in for an ERCOT deployment instruction: it makes a held ERCOT_AS award
discharge. It must only ever target one live AS award, within its product's deployment limit, and only an
operator may record one. One test per rule.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import pytest
from e2e_stack import Stack

pytestmark = pytest.mark.usefixtures("stack")

ROUTE = "/dispatch/as-deployments"
ECRS_MAX_MINUTES = 60


def _deploy(stack: Stack, body: dict, *, user: str = "operator"):
    return stack.post(ROUTE, {"reason": "e2e validation", **body}, user=user)


def _committed(stack: Stack, service_type: str, *, variant: str | None, rule: dict | None, kw: float) -> UUID:
    contract_id = stack.create_contract(service_type, "T2", variant=variant)
    if rule is not None:
        stack.add_product_rule(contract_id, **rule)
    start, end = stack.free_window(2)
    offer = stack.offer(contract_id, window_start=start, window_end=end, requested_kw=kw, value_per_mwh=200)
    obligation = stack.wait_decided(offer)
    # The intake prices hourly AS offers for every active ERCOT_AS contract; end it so only this award holds.
    stack.end_contract(contract_id)
    if obligation["state"] != "COMMITTED":
        pytest.skip(f"could not commit a {service_type} obligation on this stack: {obligation['state']}")
    return obligation["obligation_id"]


def test_an_unknown_obligation_is_404(stack: Stack) -> None:
    resp = _deploy(stack, {"obligation_id": str(uuid4()), "duration_minutes": 15})

    assert resp.status_code == 404, resp.text


def test_a_non_as_obligation_is_409(stack: Stack) -> None:
    energy = _committed(stack, "ERCOT_ENERGY", variant=None, rule=None, kw=40)

    resp = _deploy(stack, {"obligation_id": str(energy), "duration_minutes": 15})

    assert resp.status_code == 409, resp.text


def test_a_settled_obligation_is_409(stack: Stack) -> None:
    settled = stack.rows(
        "SELECT obligation_id FROM og.obligation WHERE service_type = 'ERCOT_AS' AND state = 'SETTLED' LIMIT 1"
    )
    if not settled:
        pytest.skip("no SETTLED ERCOT_AS obligation on this stack yet")

    resp = _deploy(stack, {"obligation_id": str(settled[0]["obligation_id"]), "duration_minutes": 15})

    assert resp.status_code == 409, resp.text


def test_a_duration_over_the_product_limit_is_409(stack: Stack) -> None:
    ecrs = _committed(
        stack,
        "ERCOT_AS",
        variant="ECRS",
        rule={
            "product_code": "ECRS",
            "min_qty_kw": 100,
            "increment_kw": 100,
            "block": False,
            "duration_minutes": ECRS_MAX_MINUTES,
            "variable_kind": "SEMI_CONTINUOUS",
        },
        kw=100,
    )

    resp = _deploy(stack, {"obligation_id": str(ecrs), "duration_minutes": ECRS_MAX_MINUTES + 30})

    assert resp.status_code == 409, resp.text


def test_a_fleet_wide_scope_is_409(stack: Stack) -> None:
    resp = _deploy(stack, {"scope": "ALL", "duration_minutes": 15})

    assert resp.status_code == 409, resp.text


def test_a_missing_obligation_id_is_422(stack: Stack) -> None:
    resp = _deploy(stack, {"duration_minutes": 15})

    assert resp.status_code == 422, resp.text


def test_a_viewer_cannot_deploy(stack: Stack) -> None:
    resp = _deploy(stack, {"obligation_id": str(uuid4()), "duration_minutes": 15}, user="viewer")

    assert resp.status_code == 403, resp.text
