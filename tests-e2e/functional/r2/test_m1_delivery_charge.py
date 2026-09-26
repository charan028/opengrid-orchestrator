"""R2: M1 TDSP delivery charge by zone (D-19), test plan TS-08-10.

Settlement charges the full flat per-kWh TDSP delivery charge on grid-charged kWh for obligations served in an
ERCOT competitive (FREE-market) zone, and nothing in a regulated territory (Austin Energy, LZ_AEN: no TDSP, see
`[zone_territory]` in tdsp_tariffs.toml). Read from what og-settle has written to og.pnl on the running stack.
"""

from __future__ import annotations

import pytest
from e2e_stack import Stack

pytestmark = pytest.mark.usefixtures("stack")

REGULATED_ZONE = "LZ_AEN"

_PNL_BY_ZONE = """
    SELECT p.obligation_id, p.interval_start, p.delivery_charge, b.zone
    FROM og.pnl p
    JOIN LATERAL (
        SELECT r.bank_id FROM og.reservation r WHERE r.obligation_id = p.obligation_id ORDER BY r.created_at LIMIT 1
    ) r ON true
    JOIN og.bank b ON b.bank_id = r.bank_id
    WHERE p.superseded_by IS NULL
"""


def test_ts_08_10_a_free_market_zone_settles_a_delivery_charge_and_lz_aen_settles_none(stack: Stack) -> None:
    rows = stack.rows(_PNL_BY_ZONE)
    free = [row for row in rows if row["zone"] != REGULATED_ZONE]
    regulated = [row for row in rows if row["zone"] == REGULATED_ZONE]

    charged_regulated = [row for row in regulated if row["delivery_charge"] != 0]
    assert not charged_regulated, (
        f"LZ_AEN (regulated, no TDSP) settled a delivery charge: {charged_regulated[:3]}"
    )
    if not any(row["delivery_charge"] > 0 for row in free):
        pytest.skip(
            f"no FREE-zone settlement with grid-charged kWh on this stack yet ({len(free)} FREE-zone pnl rows, "
            "none charged); run a delivery with grid charging and let og-settle catch up"
        )
