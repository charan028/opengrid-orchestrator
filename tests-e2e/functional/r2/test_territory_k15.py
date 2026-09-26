"""R2: two markets and territory (K15; D-20/D-21), test plan TS-19-03, TS-19-04/-17, TS-19-05/-31.

A REGULATED (Austin Energy) obligation is served only by LZ_AEN hubs; LZ_AEN hubs take no FREE (ERCOT) work, so
the guardian's G-33 never has to veto and the allocator withholds them with R-TERRITORY-* reasons; an obligation
whose market or territory cannot be resolved is never eligible (K15 fails closed), and CPS Energy (off in R2) gets nothing.

`POST /og/api/contracts` cannot create a REGULATED contract yet (no market/utility fields), so the contract row
is arranged in the database; everything after that goes through the running engine and guardian.
"""

from __future__ import annotations

from uuid import UUID, uuid4

import psycopg
import pytest
from e2e_stack import Stack, now_utc, wait_until

pytestmark = pytest.mark.usefixtures("stack")

AUSTIN_ZONE = "LZ_AEN"


@pytest.fixture(scope="module", autouse=True)
def austin_fleet(stack: Stack) -> None:
    hubs = stack.rows(
        "SELECT count(*) AS n FROM og.hub h JOIN og.bank b USING (bank_id) JOIN og.hub_state s USING (hub_id) "
        "WHERE b.zone = %(z)s AND s.health = 'online'",
        {"z": AUSTIN_ZONE},
    )[0]["n"]
    if not hubs:
        pytest.skip("no online LZ_AEN hubs: enable the LZ_AEN zone block in dev/config/fleet.dev.yaml")
    if not stack.rows("SELECT 1 FROM og.utility WHERE utility_id = 'AUSTIN_ENERGY'"):
        pytest.skip("og.utility has no AUSTIN_ENERGY row: apply dev/seed/market_model_seed.sql")


def _contract(stack: Stack, *, service_type: str, market: str, utility_id: str | None) -> UUID:
    contract_id = uuid4()
    stack.execute(
        """INSERT INTO og.contract (contract_id, customer_id, service_type, tier, profile_ref, start_at, status,
                                    market, utility_id, degradation_cost)
           VALUES (%(c)s, %(cu)s, %(t)s, 'T1', 'e2e-territory@1', now() - interval '1 day', 'ACTIVE',
                   %(m)s, %(u)s, 0.03)""",
        {"c": contract_id, "cu": uuid4(), "t": service_type, "m": market, "u": utility_id},
    )
    stack.created_contracts.append(contract_id)
    return contract_id


def _reserved_zones(stack: Stack, obligation_id) -> set[str]:
    return {
        row["zone"]
        for row in stack.rows(
            "SELECT DISTINCT b.zone FROM og.reservation r JOIN og.bank b USING (bank_id) "
            "WHERE r.obligation_id = %(o)s AND r.released_at IS NULL",
            {"o": obligation_id},
        )
    }


def test_ts_19_04_a_regulated_austin_obligation_is_reserved_only_on_lz_aen(stack: Stack) -> None:
    contract_id = _contract(
        stack, service_type="REGULATED_CAPACITY", market="REGULATED", utility_id="AUSTIN_ENERGY"
    )
    start, end = stack.free_window(2)
    offer = stack.offer(contract_id, window_start=start, window_end=end, requested_kw=150, value_per_mwh=150)

    obligation = stack.wait_decided(offer)

    if obligation["state"] != "COMMITTED":
        pytest.skip(
            f"the gate did not commit the REGULATED offer ({obligation['state']}): the selector withholds banks "
            "without a zone price forecast (NOT_FOR_FIRM), and on the dev stack the market simulator publishes no "
            "LZ_AEN settlement-point price; also check NO_NEW_COMMITMENTS"
        )
    zones = _reserved_zones(stack, obligation["obligation_id"])
    assert zones == {AUSTIN_ZONE}, (
        f"a REGULATED(AUSTIN_ENERGY) obligation was reserved outside LZ_AEN: {zones}"
    )


def test_ts_19_05_31_lz_aen_hubs_take_no_free_work(stack: Stack) -> None:
    started = now_utc()
    wait_until(lambda: (now_utc() - started).total_seconds() >= 30, timeout_s=40, what="30 s of dispatch")

    free_on_austin = stack.rows(
        """SELECT g.bank_id, g.granted_kw, g.is_headroom, c.market FROM og.grant g
           JOIN og.bank b USING (bank_id)
           LEFT JOIN og.obligation o USING (obligation_id)
           LEFT JOIN og.contract c ON c.contract_id = o.contract_id
           WHERE b.zone = %(z)s AND g.created_at >= %(t)s AND g.granted_kw > 0
             AND (g.is_headroom OR coalesce(c.market, 'FREE') = 'FREE')""",
        {"z": AUSTIN_ZONE, "t": started},
    )
    assert not free_on_austin, f"LZ_AEN hubs were granted FREE/headroom work: {free_on_austin[:5]}"
    g33 = stack.rows(
        "SELECT count(*) AS n FROM og.verdict v JOIN og.command_batch b USING (command_batch_id) "
        "WHERE b.created_at >= %(t)s AND 'G-33' = ANY(v.vetoed_rule_ids)",
        {"t": started},
    )[0]["n"]
    assert g33 == 0, (
        f"the guardian had to veto {g33} batches on G-33: the allocator let FREE work reach LZ_AEN"
    )


def test_ts_19_03_an_unresolvable_or_unserved_territory_is_never_eligible(stack: Stack) -> None:
    # A regulated-only service filed as FREE has no resolvable market: the data layer refuses it outright.
    with pytest.raises(psycopg.errors.CheckViolation):
        _contract(stack, service_type="REGULATED_CAPACITY", market="FREE", utility_id=None)

    # CPS Energy stays off in R2: no asset sits in its territory, so its obligation has nothing eligible.
    contract_id = _contract(
        stack, service_type="REGULATED_CAPACITY", market="REGULATED", utility_id="CPS_ENERGY"
    )
    start, end = stack.free_window(2)
    offer = stack.offer(contract_id, window_start=start, window_end=end, requested_kw=100, value_per_mwh=500)

    obligation = stack.wait_decided(offer)

    assert obligation["state"] != "COMMITTED", obligation
    assert not _reserved_zones(stack, obligation["obligation_id"]), (
        "an obligation with no territory got a reservation"
    )
