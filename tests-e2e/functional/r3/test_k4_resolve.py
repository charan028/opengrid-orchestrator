"""R3: K4 item-veto re-solve.

When the guardian vetoes one item of a bank's batch (here: a hub whose unit cap the fixture lowers below the
setpoint the engine keeps proposing), the rest of that bank is still signed in the same cycle, and the vetoed hub
is excluded for the following cycles (R-HUB-VETO-EXCLUDED). The fixture restores the hub and restarts the guardian
afterwards (the guardian reads hub ratings at startup).
"""

from __future__ import annotations

from collections.abc import Iterator

import pytest
from e2e_stack import Stack, now_utc, wait_until

pytestmark = pytest.mark.usefixtures("stack")

LOWERED_CAP_KW = 0.5


@pytest.fixture
def capped_hub(stack: Stack) -> Iterator[dict]:
    hub = stack.rows(
        """SELECT h.hub_id, h.bank_id, h.p_kw AS rated_kw FROM og.hub h JOIN og.hub_state s USING (hub_id)
           JOIN og.bank b ON b.bank_id = h.bank_id
           WHERE s.health = 'online' AND abs(s.p_kw) > %(cap)s * 4 AND b.zone NOT IN ('LZ_AEN', 'LZ_CPS')
           ORDER BY abs(s.p_kw) DESC LIMIT 1""",
        {"cap": LOWERED_CAP_KW},
    )
    if not hub:
        pytest.skip("no competitive-area hub is being dispatched above 2 kW right now")
    hub = hub[0]
    stack.execute(
        "UPDATE og.hub SET p_kw = %(c)s WHERE hub_id = %(h)s", {"c": LOWERED_CAP_KW, "h": hub["hub_id"]}
    )
    stack.compose("restart", "og-guardian")
    stack.wait_process_up("guardian")
    try:
        yield hub
    finally:
        stack.execute(
            "UPDATE og.hub SET p_kw = %(c)s WHERE hub_id = %(h)s", {"c": hub["rated_kw"], "h": hub["hub_id"]}
        )
        stack.compose("restart", "og-guardian")
        stack.wait_process_up("guardian")


def test_an_item_veto_still_signs_the_rest_of_the_bank_and_excludes_the_hub(
    stack: Stack, capped_hub: dict
) -> None:
    since = now_utc()
    bank = capped_hub["bank_id"]

    vetoed_cycle = wait_until(
        lambda: next(
            iter(
                stack.rows(
                    """SELECT b.cycle_id FROM og.verdict v JOIN og.command_batch b USING (command_batch_id)
                       WHERE b.created_at >= %(t)s AND b.submission_id LIKE %(sub)s AND v.outcome IN ('PARTLY_VETOED', 'VETOED')
                       ORDER BY b.created_at LIMIT 1""",
                    {"t": since, "sub": f"%:{bank}"},
                )
            ),
            None,
        ),
        timeout_s=60,
        what=f"an item veto on {bank} (the lowered unit cap)",
    )["cycle_id"]

    signed_same_cycle = stack.rows(
        """SELECT 1 FROM og.verdict v JOIN og.command_batch b USING (command_batch_id)
           WHERE b.cycle_id = %(c)s AND b.submission_id LIKE %(sub)s AND v.outcome = 'PASS'""",
        {"c": vetoed_cycle, "sub": f"%{bank}%"},
    )
    assert signed_same_cycle, f"the rest of {bank} was not signed in cycle {vetoed_cycle} after the item veto"

    wait_until(
        lambda: stack.rows(
            "SELECT 1 FROM og.trace WHERE created_at >= %(t)s AND 'R-HUB-VETO-EXCLUDED' = ANY(reason_codes) "
            "AND payload::text LIKE %(h)s LIMIT 1",
            {"t": since, "h": f"%{capped_hub['hub_id']}%"},
        ),
        timeout_s=60,
        what=f"{capped_hub['hub_id']} to be excluded (R-HUB-VETO-EXCLUDED)",
    )
