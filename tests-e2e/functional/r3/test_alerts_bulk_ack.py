"""R3: alerts list and bulk acknowledgement.

`GET /og/api/alerts` pages and filters alerts (and groups them with `group=true`); `POST /og/api/alerts/ack-bulk`
acknowledges 1-500 ids and reports an outcome per id. Acknowledging records who saw an alert; it never clears it.
A viewer may read but not acknowledge.
"""

from __future__ import annotations

from uuid import uuid4

import pytest
from e2e_stack import Stack

pytestmark = pytest.mark.usefixtures("stack")


def _open_alert_ids(stack: Stack, limit: int) -> list:
    return [
        row["id"]
        for row in stack.rows(
            "SELECT id FROM og.alert WHERE cleared_at IS NULL ORDER BY opened_at DESC LIMIT %(n)s",
            {"n": limit},
        )
    ]


def test_the_alert_list_pages_filters_and_groups(stack: Stack) -> None:
    first = stack.get("/alerts", limit=2, offset=0)
    assert first.status_code == 200, first.text
    grouped = stack.get("/alerts", group="true")
    assert grouped.status_code == 200, grouped.text
    filtered = stack.get("/alerts", status="open")
    assert filtered.status_code == 200, filtered.text


def test_bulk_ack_reports_an_outcome_per_id_and_does_not_clear(stack: Stack) -> None:
    ids = _open_alert_ids(stack, 3)
    if not ids:
        pytest.skip("no open alert on this stack to acknowledge")
    unknown = 2_000_000_000

    resp = stack.post("/alerts/ack-bulk", {"ids": [*ids, unknown]})

    assert resp.status_code == 200, resp.text
    body = resp.text
    for alert_id in (*ids, unknown):
        assert str(alert_id) in body, f"no per-id outcome for {alert_id}: {body[:300]}"
    still_open = stack.rows(
        "SELECT count(*) AS n FROM og.alert WHERE id = ANY(%(i)s) AND cleared_at IS NULL", {"i": ids}
    )[0]["n"]
    assert still_open == len(ids), "acknowledging must not clear an alert"
    acked = stack.rows(
        "SELECT count(*) AS n FROM og.alert WHERE id = ANY(%(i)s) AND acked_by IS NOT NULL", {"i": ids}
    )
    assert acked[0]["n"] == len(ids)


@pytest.mark.parametrize("count", [0, 501])
def test_bulk_ack_takes_between_1_and_500_ids(stack: Stack, count: int) -> None:
    resp = stack.post("/alerts/ack-bulk", {"ids": list(range(1, count + 1))})

    assert resp.status_code == 422, resp.text


def test_a_viewer_cannot_bulk_ack(stack: Stack) -> None:
    resp = stack.post("/alerts/ack-bulk", {"ids": [1]}, user="viewer")

    assert resp.status_code == 403, resp.text


def test_bulk_ack_rejects_a_non_integer_id(stack: Stack) -> None:
    resp = stack.post("/alerts/ack-bulk", {"ids": [str(uuid4())]})

    assert resp.status_code == 422, resp.text
