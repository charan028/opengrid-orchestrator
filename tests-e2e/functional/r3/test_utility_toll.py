"""R3: the Austin Energy utility toll (decision D-29).

A REGULATED_CAPACITY contract, variant TOLLING, market REGULATED, utility AUSTIN_ENERGY, served from LZ_AEN banks.
The intake creates the daily 16:30-18:00 CT obligation and commits it; it is held at 0 kW (R-GRANT-AS-HOLD) until
the utility calls it. A call (`POST /og/api/dispatch/as-deployments` with the toll's obligation) discharges up to
the committed kW, capped at 90 min (409 above it); a second overlapping call is 409. A fleet-wide ERCOT AS
deployment never touches the toll. Settlement pays availability ($/kW-yr), not kWh.

The contract row is a DB fixture until the contracts API accepts market/utility_id (R3 contracts repo).
"""

from __future__ import annotations

from datetime import datetime, time, timedelta
from uuid import UUID, uuid4
from zoneinfo import ZoneInfo

import pytest
from e2e_stack import Stack, now_utc, wait_until

pytestmark = pytest.mark.usefixtures("stack")

CT = ZoneInfo("America/Chicago")
WINDOW = (time(16, 30), time(18, 0))
TOLL_CAP_MINUTES = 90


@pytest.fixture(scope="module")
def toll(stack: Stack) -> dict:
    if not stack.rows("SELECT 1 FROM og.utility WHERE utility_id = 'AUSTIN_ENERGY'"):
        pytest.skip("og.utility has no AUSTIN_ENERGY row: apply dev/seed/market_model_seed.sql")
    contract_id = uuid4()
    stack.execute(
        """INSERT INTO og.contract (contract_id, customer_id, service_type, variant, tier, profile_ref, start_at,
                                    status, market, utility_id, degradation_cost)
           VALUES (%(c)s, %(cu)s, 'REGULATED_CAPACITY', 'TOLLING', 'T1', 'e2e-toll@1', now() - interval '1 day',
                   'ACTIVE', 'REGULATED', 'AUSTIN_ENERGY', 0.03)""",
        {"c": contract_id, "cu": uuid4()},
    )
    stack.created_contracts.append(contract_id)
    obligation = wait_until(
        lambda: next(
            iter(
                stack.rows(
                    "SELECT * FROM og.obligation WHERE contract_id = %(c)s AND state IN ('COMMITTED', 'DELIVERING') "
                    "ORDER BY window_start LIMIT 1",
                    {"c": contract_id},
                )
            ),
            None,
        ),
        timeout_s=300,
        interval_s=5,
        what="the intake to create and commit the toll's daily obligation",
    )
    return {"contract_id": contract_id, "obligation": obligation}


def _in_window(now: datetime) -> bool:
    local = now.astimezone(CT).time()
    return WINDOW[0] <= local < WINDOW[1]


def test_the_toll_obligation_is_the_daily_16_30_to_18_00_ct_window(stack: Stack, toll: dict) -> None:
    ob = toll["obligation"]
    start, end = ob["window_start"].astimezone(CT), ob["window_end"].astimezone(CT)

    assert (start.time(), end.time()) == WINDOW, (start, end)
    zones = {
        r["zone"]
        for r in stack.rows(
            "SELECT DISTINCT b.zone FROM og.reservation r JOIN og.bank b USING (bank_id) "
            "WHERE r.obligation_id = %(o)s AND r.released_at IS NULL",
            {"o": ob["obligation_id"]},
        )
    }
    assert zones <= {"LZ_AEN"}, f"the toll is reserved outside LZ_AEN: {zones}"


def _served_kw(stack: Stack, obligation_id: UUID, since) -> list:
    return [
        r["kw"]
        for r in stack.rows(
            "SELECT sum(granted_kw) AS kw FROM og.grant WHERE obligation_id = %(o)s AND created_at >= %(t)s "
            "GROUP BY cycle_id ORDER BY min(created_at)",
            {"o": obligation_id, "t": since},
        )
    ]


def test_the_toll_is_held_at_0_kw_until_called_then_a_call_discharges_it(stack: Stack, toll: dict) -> None:
    if not _in_window(now_utc()):
        pytest.skip("outside the toll's 16:30-18:00 CT window: hold and call are only observable inside it")
    ob = toll["obligation"]
    since = now_utc()
    wait_until(lambda: (now_utc() - since).total_seconds() >= 10, timeout_s=15, what="10 s of cycles")
    assert all(kw <= 0.5 for kw in _served_kw(stack, ob["obligation_id"], since)), (
        "an uncalled toll discharged"
    )

    called = stack.post(
        "/dispatch/as-deployments",
        {"obligation_id": str(ob["obligation_id"]), "duration_minutes": 15, "reason": "e2e utility call"},
    )
    assert called.status_code == 201, called.text
    try:
        overlap = stack.post(
            "/dispatch/as-deployments",
            {"obligation_id": str(ob["obligation_id"]), "duration_minutes": 15, "reason": "e2e overlap"},
        )
        assert overlap.status_code == 409, overlap.text
        wait_until(
            lambda: any(
                kw > 0.5 for kw in _served_kw(stack, ob["obligation_id"], now_utc() - timedelta(seconds=6))
            ),
            timeout_s=45,
            what="the called toll to discharge",
        )
        committed = float(ob["committed_qty_kw"])
        assert all(float(kw) <= committed + 0.5 for kw in _served_kw(stack, ob["obligation_id"], since))
    finally:
        stack.http.delete(
            f"{stack.api_base}/dispatch/as-deployments/{called.json()['deployment_id']}",
            headers=stack.headers(),
        )


def test_a_toll_call_over_90_minutes_is_409(stack: Stack, toll: dict) -> None:
    resp = stack.post(
        "/dispatch/as-deployments",
        {
            "obligation_id": str(toll["obligation"]["obligation_id"]),
            "duration_minutes": TOLL_CAP_MINUTES + 30,
            "reason": "e2e cap",
        },
    )

    assert resp.status_code == 409, resp.text


def test_an_ercot_as_deployment_never_touches_the_toll(stack: Stack, toll: dict) -> None:
    ercot = stack.rows(
        "SELECT obligation_id FROM og.obligation WHERE service_type = 'ERCOT_AS' AND state IN ('COMMITTED', 'DELIVERING') "
        "LIMIT 1"
    )
    if not ercot:
        pytest.skip("no held ERCOT_AS award on this stack to deploy")
    since = now_utc()
    deployed = stack.post(
        "/dispatch/as-deployments",
        {
            "obligation_id": str(ercot[0]["obligation_id"]),
            "duration_minutes": 5,
            "reason": "e2e ERCOT deployment",
        },
    )
    try:
        wait_until(lambda: (now_utc() - since).total_seconds() >= 12, timeout_s=20, what="12 s of cycles")
        assert all(kw <= 0.5 for kw in _served_kw(stack, toll["obligation"]["obligation_id"], since)), (
            "an ERCOT AS deployment discharged the utility toll"
        )
    finally:
        if deployed.status_code == 201:
            stack.http.delete(
                f"{stack.api_base}/dispatch/as-deployments/{deployed.json()['deployment_id']}",
                headers=stack.headers(),
            )


def test_the_toll_settles_availability_not_energy(stack: Stack, toll: dict) -> None:
    rows = stack.rows(
        "SELECT revenue, interval_start FROM og.pnl WHERE obligation_id = %(o)s AND superseded_by IS NULL",
        {"o": toll["obligation"]["obligation_id"]},
    )
    if not rows:
        pytest.skip("the toll has not been settled yet on this stack (settlement runs after its window)")
    assert all(row["revenue"] > 0 for row in rows), (
        f"an available, uncalled toll must still earn availability: {rows}"
    )
