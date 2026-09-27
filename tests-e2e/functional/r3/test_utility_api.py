"""D-33 (r3.4.1): the utility customer API, driven the way Austin Energy's own account drives it.

`og-util-aen` (`[api.roles.utility]` -> AUSTIN_ENERGY, listed in `[api.utility_api] enabled_utilities`) reads its
tolling obligations and issues, reads and cancels toll calls under `/og/api/customer/v1/utility/`. Requests go
out as that Apache account the way every functional suite sends them: `X-Remote-User` plus the proxy secret
(`Stack.headers(user)`). `og.*` rows are read only to check scope and what an operator sees.

The call status passes on r3.4.2 and on r3.4.3 alike, without guessing the release. The assertions read which
fields the status carries:
- r3.4.2 has the allocator's `granted_kw`, `delivery_measured` is always false, and a running call is ACTIVE.
- r3.4.3 (D-38) has the measured `delivered_kw`. A running call is ACTIVE until a measured bucket gives it a
  delivered kW, then RAMPING or DELIVERING.
Where r3.4.3 changed a reason code (cancelling an ended call, or a call the utility did not issue), r3.4.2's code is
accepted on r3.4.2. The r3.4.3-only checks (a timestamp without a UTC offset is 422, a refused read is traced) skip
on r3.4.2.

Nothing here discharges outside the toll window:
- The scheduled-call scenario starts its call a few minutes ahead and cancels it before it starts.
- The probes of foreign or unknown obligations start a week ahead, outside every window, so even a broken scope
  check could deploy nothing.
- Only the two in-window scenarios run a call now: inside the 16:30-18:00 CT window, for 1 MW. One ends its call
  at once. The slow one (r3.4.3) ends it after the first measured bucket, about a minute in.

A run adds at most seven rows to og-util-aen's hourly call budget. It raises the alerts a real call raises
(ALR-UTILITY-CALL, ALR-UTILITY-CALL-REFUSED).

Not covered: the per-account rate limit (`[dispatch.calls]`, 30 calls an hour). Reaching it takes 30 calls and
then refuses every call from og-util-aen, the Austin Energy simulator's own account, for up to an hour.

Skips, not failures, when:
- this stack does not serve the utility API to og-util-aen (`[api.customer_api].enabled`, the
  `[api.roles.utility]` mapping or `enabled_utilities` missing; dev/config/docker.toml sets none of them);
- og.utility has no AUSTIN_ENERGY row;
- no deployable Austin Energy toll window has room for a call.
"""

from __future__ import annotations

from datetime import UTC, datetime, time, timedelta
from typing import Any
from uuid import uuid4
from zoneinfo import ZoneInfo

import httpx
import pytest
from e2e_stack import Stack, now_utc, wait_until

pytestmark = pytest.mark.usefixtures("stack")

BASE = "/customer/v1/utility"
UTILITY_USER = "og-util-aen"
UTILITY_ID = "AUSTIN_ENERGY"
CT = ZoneInfo("America/Chicago")
#: Obligation states a call can deploy (`opengrid.calls.rules.DEPLOYABLE_STATES`).
DEPLOYABLE = frozenset({"COMMITTED", "DELIVERING", "SHORTFALL"})
#: `delivery_state` once og-settle's delivery job has a record of the call (r3.4.3, `DeliveryResult`).
DELIVERY_RESULTS = frozenset({"IN_PROGRESS", "PASS", "PARTIAL", "FAIL"})
#: 403 details meaning "this stack is not set up for og-util-aen" (`api.auth`, `customer_api.utility_identity`).
NOT_CONFIGURED = (
    "No role mapped for identity",
    "no utility_id mapped for this identity",
    "the utility API is not enabled for this utility",
)
#: A scheduled call starts at least this far ahead, so it is cancelled long before it could start.
LEAD = timedelta(minutes=5)
CALL_MINUTES = 5
#: In-window calls: short, and 1 MW against the 24 MW toll.
RUN_MINUTES = 2
MEASURED_RUN_MINUTES = 4
CALL_KW = 1000.0
#: 30 s buckets, evaluated 30 s late, by a job running every 15 s: a first bucket after about a minute.
MEASURE_TIMEOUT_S = 150.0
OBLIGATION_DAYS = 14  # the most `GET obligations` accepts


def _get(stack: Stack, path: str, **params: Any) -> httpx.Response:
    return stack.get(f"{BASE}{path}", user=UTILITY_USER, **params)


def _post(stack: Stack, path: str, body: dict[str, Any], *, user: str = UTILITY_USER) -> httpx.Response:
    return stack.post(f"{BASE}{path}", body, user=user)


def _call_body(
    obligation_id: str, *, start_at: datetime | None, minutes: int = CALL_MINUTES
) -> dict[str, Any]:
    body: dict[str, Any] = {
        "kw": -CALL_KW,
        "duration_minutes": minutes,
        "obligation_id": obligation_id,
        "idempotency_key": f"e2e-{uuid4()}",
        "reason": "e2e utility API",
    }
    if start_at is not None:
        body["start_at"] = start_at.isoformat()
    return body


def _probe(stack: Stack, obligation_id: str, *, user: str = UTILITY_USER) -> httpx.Response:
    """A call a week ahead: outside every toll window, so it deploys nothing even if scoping were broken."""
    return _post(
        stack, "/calls", _call_body(obligation_id, start_at=now_utc() + timedelta(days=7)), user=user
    )


def _skip_if_taken(resp: httpx.Response) -> None:
    detail = resp.json().get("detail") if resp.status_code == 409 else None
    if isinstance(detail, dict) and detail.get("reason_code") == "R-CALL-OVERLAP":
        pytest.skip(f"another call already covers this toll window: {detail.get('detail')}")


def _measures_delivery(status: dict[str, Any]) -> bool:
    """r3.4.3 (D-38) reports the measured `delivered_kw`; r3.4.2 reports only the allocator's `granted_kw`."""
    return "delivered_kw" in status


def _assert_delivery(status: dict[str, Any]) -> None:
    """The delivery fields agree with each other, on either release. `granted_*` is only required when there is
    no `delivered_kw`: it is deprecated in r3.4.3 and removed in r3.5."""
    if not _measures_delivery(status):
        assert {"granted_kw", "granted_kwh"} <= status.keys(), status
        assert status["delivery_measured"] is False and status["delivery_state"] == "UNMEASURED", status
    elif status["delivery_measured"]:
        assert status["delivery_state"] in DELIVERY_RESULTS and status["delivery_as_of"], status
    else:
        assert status["delivery_state"] == "UNMEASURED" and status["delivered_kw"] is None, status
    if (
        "granted_description" in status
    ):  # r3.4.2 "planned/granted, ..."; r3.4.3 "planned; removed in r3.5; ..."
        assert status["granted_description"].startswith("planned"), status


def _energy(status: dict[str, Any]) -> tuple[Any, Any]:
    """(kW, kWh): measured on r3.4.3, planned/granted on r3.4.2."""
    if _measures_delivery(status):
        return status["delivered_kw"], status["delivered_kwh"]
    return status["granted_kw"], status["granted_kwh"]


def _assert_not_started(status: dict[str, Any], state: str) -> None:
    """Before its start a call is ACCEPTED (COMPLETED once cancelled), and nothing is measured or granted."""
    assert status["state"] == state, status
    _assert_delivery(status)
    assert status["delivery_measured"] is False and _energy(status) == (None, 0.0), status


def _assert_running(status: dict[str, Any]) -> None:
    """Unmeasured (always on r3.4.2): ACTIVE with delivery_state UNMEASURED. Measured (r3.4.3, D-38): a delivered
    kW exists and the state is RAMPING or DELIVERING."""
    _assert_delivery(status)
    if status["delivery_measured"]:
        assert status["delivered_kw"] is not None and status["state"] in {"RAMPING", "DELIVERING"}, status
    else:
        assert status["state"] == "ACTIVE" and status["delivery_state"] == "UNMEASURED", status


def _deployable(ob: dict[str, Any], minutes: int) -> bool:
    return (
        ob["state"] in DEPLOYABLE
        and bool(ob["window_start"] and ob["window_end"])
        and (ob["max_call_minutes"] or 0) >= minutes
        and (ob["committed_kw"] or 0.0) >= CALL_KW
    )


@pytest.fixture(scope="module")
def utility(stack: Stack) -> dict[str, Any]:
    """`GET me` as og-util-aen; skips when this stack does not serve the utility API to that account."""
    resp = _get(stack, "/me")
    if resp.status_code == 404:
        pytest.skip(
            "og-api does not mount /og/api/customer/v1/utility/ here: set [api.customer_api] enabled = true"
        )
    detail = str(resp.json().get("detail", "")) if resp.status_code == 403 else ""
    if detail.startswith(NOT_CONFIGURED):
        pytest.skip(
            f"{UTILITY_USER} may not use the utility API on this stack ({detail}): map it in [api.roles.utility] "
            f"and list {UTILITY_ID} in [api.utility_api] enabled_utilities"
        )
    assert resp.status_code == 200, resp.text
    return resp.json()


@pytest.fixture(scope="module")
def austin(stack: Stack, utility: dict[str, Any]) -> dict[str, Any]:
    if not stack.rows("SELECT 1 FROM og.utility WHERE utility_id = %(u)s", {"u": UTILITY_ID}):
        pytest.skip("og.utility has no AUSTIN_ENERGY row: apply dev/seed/market_model_seed.sql")
    return utility


@pytest.fixture(scope="module")
def measures_delivery(stack: Stack, utility: dict[str, Any]) -> bool:
    """True from r3.4.3 on: the call status carries the measured `delivered_kw` (D-38). Read from the status of
    the utility's newest call, or of a refused probe when it has none."""
    history = _get(stack, "/calls", limit=1)
    assert history.status_code == 200, history.text
    calls = history.json()["calls"]
    call_id = calls[0]["call_id"] if calls else _probe(stack, str(uuid4())).json()["detail"]["call_id"]
    status = _get(stack, f"/calls/{call_id}")
    assert status.status_code == 200, status.text
    return _measures_delivery(status.json())


def test_me_names_the_callers_utility(utility: dict[str, Any]) -> None:
    assert utility == {"user": UTILITY_USER, "utility_id": UTILITY_ID, "api_version": "v1"}


@pytest.mark.parametrize("user", ["operator", "viewer", "e2e-not-a-utility"])
def test_a_non_utility_identity_is_refused(stack: Stack, utility: dict[str, Any], user: str) -> None:
    for resp in (
        stack.get(f"{BASE}/me", user=user),
        stack.get(f"{BASE}/obligations", user=user),
        stack.get(f"{BASE}/calls", user=user),
        _probe(stack, str(uuid4()), user=user),
    ):
        assert resp.status_code == 403, (user, str(resp.request.url), resp.text)


def test_a_refused_read_is_traced_as_authz_deny(stack: Stack, measures_delivery: bool) -> None:
    if not measures_delivery:
        pytest.skip("r3.4.2 traces refused calls and cancels only, not refused reads")
    since = stack.rows("SELECT now() - interval '5 seconds' AS t")[0]["t"]

    assert stack.get(f"{BASE}/me", user="viewer").status_code == 403

    assert stack.rows(
        """SELECT 1 FROM og.trace WHERE stream_id = 'authz_deny:viewer' AND event_class = 'TRACE_AUTHZ_DENY'
             AND payload ->> 'action' = 'utility.read' AND created_at >= %(t)s""",
        {"t": since},
    ), "the refused utility.read was not traced as AUTHZ_DENY"


def test_the_utility_account_is_refused_by_the_operator_api(stack: Stack, utility: dict[str, Any]) -> None:
    for path in ("/fleet/summary", "/dispatch/as-deployments", "/dispatch/calls"):
        assert stack.get(path, user=UTILITY_USER).status_code == 403, path


def _tolling_obligations(stack: Stack, since: datetime, until: datetime) -> dict[str, str | None]:
    """obligation_id -> contract utility_id, for every tolling obligation whose window starts in [since, until)."""
    rows = stack.rows(
        """SELECT o.obligation_id, c.utility_id FROM og.obligation o JOIN og.contract c ON c.contract_id = o.contract_id
           WHERE o.service_type = 'REGULATED_CAPACITY' AND upper(coalesce(c.variant, '')) = 'TOLLING'
             AND o.window_start >= %(a)s AND o.window_start < %(b)s""",
        {"a": since, "b": until},
    )
    return {str(row["obligation_id"]): row["utility_id"] for row in rows}


def test_obligations_lists_only_its_own_tolling_obligations(stack: Stack, austin: dict[str, Any]) -> None:
    local_day = now_utc().astimezone(CT).date()
    since = datetime.combine(local_day, time.min, tzinfo=CT).astimezone(UTC)  # the API's own day bounds
    until = since + timedelta(days=OBLIGATION_DAYS)
    before = _tolling_obligations(stack, since, until)

    resp = _get(stack, "/obligations", days=OBLIGATION_DAYS)
    after = _tolling_obligations(stack, since, until)

    assert resp.status_code == 200, resp.text
    body = resp.json()
    listed = {ob["obligation_id"] for ob in body["obligations"]}
    mine_before = {oid for oid, owner in before.items() if owner == UTILITY_ID}
    mine_after = {oid for oid, owner in after.items() if owner == UTILITY_ID}
    assert body["utility_id"] == UTILITY_ID
    assert mine_before <= listed <= mine_after, (sorted(listed), sorted(mine_after))
    assert not listed & {oid for oid, owner in after.items() if owner != UTILITY_ID}, "another utility's toll"
    for ob in body["obligations"]:
        assert ob["utility_id"] == UTILITY_ID, ob
        assert ob["service_type"] == "REGULATED_CAPACITY" and (ob["variant"] or "").upper() == "TOLLING", ob
        if ob["active_call_id"] is None:
            assert ob["held_kw"] == 0.0, ob  # D-29: held at 0 kW until called
    if body["today"] is not None:
        assert body["today"] in body["obligations"]
        assert datetime.fromisoformat(body["today"]["window_start"]).astimezone(CT).date() == local_day


def test_a_call_on_an_unknown_obligation_is_404(stack: Stack, utility: dict[str, Any]) -> None:
    resp = _probe(stack, str(uuid4()))

    assert resp.status_code == 404, resp.text
    detail = resp.json()["detail"]
    assert (detail["reason_code"], detail["detail"]) == ("R-CALL-NOT-FOUND", "no such obligation")
    refused = _get(stack, f"/calls/{detail['call_id']}")  # the refusal is recorded as the caller's own call
    assert refused.status_code == 200, refused.text
    assert (refused.json()["state"], refused.json()["reason_code"]) == ("REFUSED", "R-CALL-NOT-FOUND")


def test_a_call_on_another_utilitys_obligation_is_404_not_403(stack: Stack, utility: dict[str, Any]) -> None:
    foreign = stack.rows(
        """SELECT o.obligation_id FROM og.obligation o JOIN og.contract c ON c.contract_id = o.contract_id
           WHERE c.utility_id IS DISTINCT FROM %(u)s
           ORDER BY (c.utility_id IS NOT NULL) DESC, o.window_start DESC NULLS LAST
           LIMIT 1""",
        {"u": UTILITY_ID},
    )
    if not foreign:
        pytest.skip("no obligation of another utility, or of the free market, on this stack to probe")

    theirs = _probe(stack, str(foreign[0]["obligation_id"]))
    unknown = _probe(stack, str(uuid4()))

    assert theirs.status_code == 404, theirs.text  # never 403: another utility's obligation is not revealed
    assert unknown.status_code == 404, unknown.text
    seen, missing = theirs.json()["detail"], unknown.json()["detail"]
    assert (seen["reason_code"], seen["detail"]) == (missing["reason_code"], missing["detail"])


def test_an_unknown_call_is_404_on_read_and_cancel(stack: Stack, utility: dict[str, Any]) -> None:
    call_id = uuid4()
    for resp in (_get(stack, f"/calls/{call_id}"), _post(stack, f"/calls/{call_id}/cancel", {})):
        assert resp.status_code == 404, resp.text
        assert resp.json()["detail"] == {"reason_code": "R-CALL-NOT-FOUND", "detail": "no such call"}


def test_another_utilitys_call_is_404_like_an_unknown_one(stack: Stack, utility: dict[str, Any]) -> None:
    # A finished or refused call only: even a broken scope check could then cancel nothing that runs.
    foreign = stack.rows(
        """SELECT call_id FROM og.dispatch_call
           WHERE utility_id IS DISTINCT FROM %(u)s AND (outcome = 'REFUSED' OR end_at < now())
           ORDER BY created_at DESC LIMIT 1""",
        {"u": UTILITY_ID},
    )
    if not foreign:
        pytest.skip("no finished call of another utility or origin in og.dispatch_call to probe")
    call_id = str(foreign[0]["call_id"])

    read = _get(stack, f"/calls/{call_id}")
    cancel = _post(stack, f"/calls/{call_id}/cancel", {})
    history = _get(stack, "/calls", limit=500)

    for resp in (read, cancel):
        assert resp.status_code == 404, resp.text  # never 403
        assert resp.json()["detail"] == {"reason_code": "R-CALL-NOT-FOUND", "detail": "no such call"}
    assert history.status_code == 200, history.text
    assert call_id not in {c["call_id"] for c in history.json()["calls"]}


def test_a_timestamp_without_a_utc_offset_is_422(stack: Stack, measures_delivery: bool) -> None:
    if not measures_delivery:
        pytest.skip("r3.4.2 accepts a timestamp without a UTC offset in the schema; r3.4.3 answers 422")
    naive = (now_utc() + timedelta(days=7)).replace(tzinfo=None).isoformat()
    body = {**_call_body(str(uuid4()), start_at=None), "start_at": naive}

    for resp in (
        _post(stack, "/calls", body),
        _post(stack, f"/calls/{uuid4()}/cancel", {"end_at": naive}),
        _get(stack, "/calls", since=naive),
    ):
        assert resp.status_code == 422 and "timezone" in resp.text, (str(resp.request.url), resp.text)


def test_cancelling_an_ended_call_is_409(stack: Stack, measures_delivery: bool) -> None:
    ended = stack.rows(
        """SELECT call_id FROM og.dispatch_call
           WHERE principal = %(p)s AND origin = 'UTILITY' AND outcome = 'ACCEPTED'
             AND deployment_id IS NOT NULL AND end_at < now()
           ORDER BY created_at DESC LIMIT 1""",
        {"p": UTILITY_USER},
    )
    if not ended:
        pytest.skip(f"{UTILITY_USER} has no ended call on this stack yet")

    resp = _post(stack, f"/calls/{ended[0]['call_id']}/cancel", {})

    assert resp.status_code == 409, resp.text
    expected = "R-CALL-ALREADY-ENDED" if measures_delivery else "R-CALL-CANNOT-EXTEND"  # r3.4.3 : r3.4.2
    assert resp.json()["detail"]["reason_code"] == expected, resp.text


def test_the_utility_reads_but_cannot_cancel_a_call_it_did_not_issue(
    stack: Stack, measures_delivery: bool
) -> None:
    # An ended operator (or grid-link) call on the utility's own toll: nothing runs, whatever the answer.
    theirs = stack.rows(
        """SELECT call_id FROM og.dispatch_call
           WHERE utility_id = %(u)s AND origin <> 'UTILITY' AND outcome = 'ACCEPTED'
             AND deployment_id IS NOT NULL AND end_at < now()
           ORDER BY created_at DESC LIMIT 1""",
        {"u": UTILITY_ID},
    )
    if not theirs:
        pytest.skip("no ended operator or grid-link call on the Austin Energy toll on this stack")
    call_id = str(theirs[0]["call_id"])

    read = _get(stack, f"/calls/{call_id}")
    cancel = _post(stack, f"/calls/{call_id}/cancel", {})

    assert read.status_code == 200, read.text  # a utility reads every call on its own toll
    if measures_delivery:  # r3.4.3: only the issuer may cancel or shorten
        assert cancel.status_code == 403, cancel.text
        assert cancel.json()["detail"]["reason_code"] == "R-CALL-NOT-ISSUER", cancel.text
    else:  # r3.4.2: any call on its toll may be cancelled, but this one has ended
        assert cancel.status_code == 409, cancel.text
        assert cancel.json()["detail"]["reason_code"] == "R-CALL-CANNOT-EXTEND", cancel.text


def _schedulable_toll(stack: Stack) -> tuple[dict[str, Any], datetime]:
    """A deployable toll of today or tomorrow, and a whole-minute start at least LEAD ahead inside its window
    with room for the call; skips when there is none."""
    resp = _get(stack, "/obligations")
    assert resp.status_code == 200, resp.text
    now = now_utc()
    for ob in resp.json()["obligations"]:
        if not _deployable(ob, CALL_MINUTES):
            continue
        start = max(datetime.fromisoformat(ob["window_start"]), now + LEAD).astimezone(UTC)
        start = start.replace(second=0, microsecond=0) + timedelta(minutes=1)
        if start + timedelta(minutes=CALL_MINUTES) <= datetime.fromisoformat(ob["window_end"]):
            return ob, start
    pytest.skip(
        "no deployable AUSTIN_ENERGY toll (COMMITTED/DELIVERING/SHORTFALL) with room for a call today or tomorrow: "
        "apply dev/seed/market_model_seed.sql and let the intake and the selector commit a window"
    )


def _running_toll(stack: Stack, minutes: int) -> dict[str, Any]:
    """The toll whose window is open now with room for a `minutes` call and no call on it; skips otherwise."""
    resp = _get(stack, "/obligations")
    assert resp.status_code == 200, resp.text
    now = now_utc()
    for ob in resp.json()["obligations"]:
        if (
            _deployable(ob, minutes)
            and ob["active_call_id"] is None
            and datetime.fromisoformat(ob["window_start"]) <= now - timedelta(minutes=1)
            and datetime.fromisoformat(ob["window_end"]) >= now + timedelta(minutes=minutes + 1)
        ):
            return ob
    pytest.skip(
        "no Austin Energy toll window open now (16:30-18:00 CT) with room for a call and no call on it: "
        "a call runs only inside its window"
    )


def test_a_scheduled_call_is_created_read_and_cancelled_before_it_starts(
    stack: Stack, austin: dict[str, Any]
) -> None:
    ob, start = _schedulable_toll(stack)
    body = _call_body(ob["obligation_id"], start_at=start)

    created = _post(stack, "/calls", body)
    _skip_if_taken(created)
    assert created.status_code == 201, created.text
    call = created.json()
    cancelled = None
    try:
        assert (call["outcome"], call["origin"], call["kind"]) == ("ACCEPTED", "UTILITY", "UTILITY_CALL")
        assert (call["principal"], call["utility_id"]) == (UTILITY_USER, UTILITY_ID)
        assert call["obligation_id"] == ob["obligation_id"] and call["deployment_id"]
        assert call["target_kw"] == -CALL_KW and datetime.fromisoformat(call["start_at"]) == start
        assert call["reason"] == "utility call: e2e utility API" and call["replayed"] is False
        _assert_not_started(call, "ACCEPTED")

        replay = _post(stack, "/calls", body)  # same key, same request: the original call, nothing new
        assert replay.status_code == 200, replay.text
        assert replay.json()["call_id"] == call["call_id"] and replay.json()["replayed"] is True

        status = _get(stack, f"/calls/{call['call_id']}")
        assert status.status_code == 200, status.text
        _assert_not_started(status.json(), "ACCEPTED")
        history = _get(stack, "/calls")
        assert call["call_id"] in {c["call_id"] for c in history.json()["calls"]}

        # What an operator sees: the deployment with its origin and requester, and the alert.
        deployments = stack.get("/dispatch/as-deployments").json()
        mine = [d for d in deployments if d["call_id"] == call["call_id"]]
        assert len(mine) == 1, deployments
        assert (mine[0]["source"], mine[0]["requested_by"]) == ("UTILITY", UTILITY_USER)
        assert stack.rows(
            "SELECT 1 FROM og.alert WHERE rule = 'ALR-UTILITY-CALL' AND scope_ref = %(c)s",
            {"c": call["call_id"]},
        ), "no ALR-UTILITY-CALL for the call"

        cancelled = _post(stack, f"/calls/{call['call_id']}/cancel", {})
        assert cancelled.status_code == 200, cancelled.text
        _assert_not_started(cancelled.json(), "COMPLETED")
        assert cancelled.json()["cancelled_at"]
        again = _post(stack, f"/calls/{call['call_id']}/cancel", {})
        assert again.status_code == 409, again.text
        assert again.json()["detail"]["reason_code"] == "R-CALL-ALREADY-ENDED"
    finally:
        if cancelled is None or cancelled.status_code != 200:
            _post(stack, f"/calls/{call['call_id']}/cancel", {})


def test_a_running_call_is_active_until_its_delivery_is_measured(
    stack: Stack, austin: dict[str, Any]
) -> None:
    ob = _running_toll(stack, RUN_MINUTES)

    created = _post(stack, "/calls", _call_body(ob["obligation_id"], start_at=None, minutes=RUN_MINUTES))
    _skip_if_taken(created)
    assert created.status_code == 201, created.text
    call_id = created.json()["call_id"]
    ended = None
    try:
        _assert_running(created.json())
        status = _get(stack, f"/calls/{call_id}")
        assert status.status_code == 200, status.text
        _assert_running(status.json())

        ended = _post(stack, f"/calls/{call_id}/cancel", {})
        assert ended.status_code == 200, ended.text
        assert ended.json()["state"] == "COMPLETED", ended.json()
        _assert_delivery(ended.json())
    finally:
        if ended is None or ended.status_code != 200:
            _post(stack, f"/calls/{call_id}/cancel", {})


@pytest.mark.slow
def test_a_running_calls_state_follows_its_measured_delivery(stack: Stack, austin: dict[str, Any]) -> None:
    ob = _running_toll(stack, MEASURED_RUN_MINUTES)

    created = _post(
        stack, "/calls", _call_body(ob["obligation_id"], start_at=None, minutes=MEASURED_RUN_MINUTES)
    )
    _skip_if_taken(created)
    assert created.status_code == 201, created.text
    call_id = created.json()["call_id"]
    ended = None
    try:
        if not _measures_delivery(created.json()):
            pytest.skip("this og-api reports no measured delivery (r3.4.2: granted_kw only, always ACTIVE)")

        def measured() -> dict[str, Any] | None:
            status = _get(stack, f"/calls/{call_id}").json()
            return status if status["delivery_measured"] and status["delivered_kw"] is not None else None

        try:
            status = wait_until(
                measured, timeout_s=MEASURE_TIMEOUT_S, interval_s=5.0, what="a measured delivery bucket"
            )
        except AssertionError:
            pytest.skip(
                f"no measured bucket within {MEASURE_TIMEOUT_S:.0f} s: is og-settle's delivery job ([delivery]) running?"
            )
        _assert_running(status)  # RAMPING or DELIVERING, from the measured kW against the call's target
        assert status["delivery_state"] == "IN_PROGRESS" and status["delivered_kwh"] >= 0.0, status

        ended = _post(stack, f"/calls/{call_id}/cancel", {})
        assert ended.status_code == 200, ended.text
        assert ended.json()["state"] == "COMPLETED", ended.json()
        _assert_delivery(ended.json())
    finally:
        if ended is None or ended.status_code != 200:
            _post(stack, f"/calls/{call_id}/cancel", {})
