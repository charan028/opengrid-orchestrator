"""R2: NO_NEW_COMMITMENTS degraded mode (02b S6.5 row 1), test plan TS-07-02.

A stale ERCOT price feed puts the system in NO_NEW_COMMITMENTS: while it is active no new og.commitment row is
created (the selector logs R-DEGRADED-NO-NEW-COMMIT) and new offers stay unselected, while existing commitments
keep their rows (K13); when the feed recovers the mode clears and commitments resume. The stale feed is made
through the market simulator's control plane (`stale_posting` on the settlement-point price product). Reaching
STALE takes the feed's freshness budget (10 min on the dev stack), so this is `slow`.
"""

from __future__ import annotations

import pytest
from e2e_stack import Stack, now_utc, wait_until

pytestmark = [pytest.mark.slow, pytest.mark.usefixtures("stack")]

PRICE_PRODUCT = "np6-905-cd"
MODE = "NO_NEW_COMMITMENTS"


def _modes(stack: Stack) -> list[str]:
    """The degraded modes in force (`og.degraded_mode_state`, the table og-api's health payload reads)."""
    return [row["mode"] for row in stack.rows("SELECT mode FROM og.degraded_mode_state")]


def _commitments_since(stack: Stack, since) -> int:
    return stack.rows("SELECT count(*) AS n FROM og.commitment WHERE created_at >= %(t)s", {"t": since})[0][
        "n"
    ]


def test_ts_07_02_a_stale_price_feed_stops_new_commitments_until_it_recovers(stack: Stack) -> None:
    if MODE in _modes(stack):
        pytest.skip(f"{MODE} is already active before the test (e.g. a feed the simulator does not serve)")
    existing = stack.rows(
        "SELECT count(*) AS n FROM og.commitment c JOIN og.obligation o USING (obligation_id) "
        "WHERE o.state IN ('COMMITTED', 'DELIVERING') AND c.supersedes IS NULL"
    )[0]["n"]

    anomaly = stack.inject("stale_posting", PRICE_PRODUCT, duration_s=1800)
    try:
        wait_until(
            lambda: MODE in _modes(stack), timeout_s=900, interval_s=10, what=f"{MODE} after a stale feed"
        )
        entered = now_utc()

        contract_id = stack.create_contract("ERCOT_ENERGY", "T2")
        start, end = stack.free_window(2)
        offer = stack.offer(
            contract_id, window_start=start, window_end=end, requested_kw=40, value_per_mwh=300
        )
        during = stack.wait_decided(offer)

        assert during["state"] != "COMMITTED", "a new commitment was made during NO_NEW_COMMITMENTS"
        assert _commitments_since(stack, entered) == 0, (
            "og.commitment rows were created during NO_NEW_COMMITMENTS"
        )
        still = stack.rows(
            "SELECT count(*) AS n FROM og.commitment c JOIN og.obligation o USING (obligation_id) "
            "WHERE o.state IN ('COMMITTED', 'DELIVERING', 'SHORTFALL') AND c.supersedes IS NULL"
        )[0]["n"]
        assert still >= existing, "existing commitments were dropped by the degraded mode (K13)"
    finally:
        stack.clear_anomaly(anomaly)

    wait_until(lambda: MODE not in _modes(stack), timeout_s=900, interval_s=10, what=f"{MODE} to clear")
    recovered = stack.create_contract("ERCOT_ENERGY", "T2")
    start, end = stack.free_window(2)
    offer = stack.offer(recovered, window_start=start, window_end=end, requested_kw=40, value_per_mwh=300)
    assert stack.wait_decided(offer)["state"] == "COMMITTED", (
        "commitments did not resume after the feed recovered"
    )
