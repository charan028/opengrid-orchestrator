"""r3.4.4 live HIGH (ECRS deployment 046a2ebb, 2026-09-27 05:09:33): a 0.3 MW ERCOT deployment of a 0.5 MW ECRS award
split over banks delivered 28% -- 135 of 276 verdicts vetoed, every one G-19 R-COMMIT-LOCK-VIOLATION on the bank with
the SMALLER share (149.576 kW on bank-034 vs 150.424 kW on bank-027).

Root cause: G-19 judges ONE bank's batch -- that bank's grant against that bank's reservation -- but read the
"prior grant" obligation-wide (the latest og.grant row of ANY bank, and of the current cycle, whose grants are
persisted before the verdict). The other bank's larger share became the floor. Fixed: the prior is this bank's own
previous-cycle grant; and while a PARTIAL deployment of a capacity hold (ERCOT_AS, REGULATED_CAPACITY) is active,
the lock is this bank's share of the deployed kW, not its whole reservation.
"""

from __future__ import annotations

from dataclasses import replace
from decimal import Decimal
from uuid import UUID, uuid4

import pytest

from opengrid.guardian import repo
from opengrid.guardian.ports import ActiveObligation, ProposedItem

from .conftest import make_batch_row, make_proposal, service_with, wire_default_passing_scenario
from .test_repo import FakeCursor, FakePool

AWARD_KW = Decimal("500")
REQUESTED_KW = Decimal("300")


class _Commitments:
    """Per-bank reservations of one obligation (og.reservation), as `PgCommitmentPort` reads them."""

    def __init__(self, obligation_id: UUID, reserved_by_bank: dict[str, Decimal]) -> None:
        self.obligation_id = obligation_id
        self.reserved_by_bank = reserved_by_bank

    async def active_kw(self, obligation_id, cycle_id):
        return sum(self.reserved_by_bank.values(), Decimal(0))

    async def active_obligations_for_bank(self, bank_id, cycle_id):
        if bank_id not in self.reserved_by_bank:
            return []
        total = sum(self.reserved_by_bank.values(), Decimal(0))
        return [ActiveObligation(self.obligation_id, self.reserved_by_bank[bank_id], total)]


class _Awards:
    def __init__(self, service_type: str, *, deployed: bool, requested: Decimal | None) -> None:
        self._type, self._deployed, self._requested = service_type, deployed, requested

    async def service_type(self, obligation_id):
        return self._type

    async def deployment_active(self, obligation_id):
        return self._deployed

    async def deployment_requested_kw(self, obligation_id):
        return self._requested if self._deployed else None


class _ObligationWidePrior:
    """The pre-fix read: the latest grant row of the obligation, whatever the bank (and cycle)."""

    def __init__(self, latest: Decimal) -> None:
        self.latest = latest

    async def prior_granted_kw(self, obligation_id, bank_id=None, cycle_id=None):
        return self.latest


def _world(
    fakes,
    config,
    seed,
    reserved: dict[str, Decimal],
    *,
    service_type="ERCOT_AS",
    requested=REQUESTED_KW,
    deployed: bool = True,
):
    obligation_id = uuid4()
    service = service_with(fakes, config, seed)
    service.ports = replace(
        fakes.as_ports(),
        commitments=_Commitments(obligation_id, reserved),
        as_awards=_Awards(service_type, deployed=deployed, requested=requested),
    )
    return service, obligation_id


async def _judge(
    fakes,
    service,
    obligation_id: UUID,
    bank_id: str,
    granted: Decimal,
    cycle: int,
    seq: int,
    reason: str = "R-GRANT-COMMITTED",
):
    proposal = replace(
        make_proposal(bank_id=bank_id, seq=seq, obligation_id=obligation_id, obligation_granted_kw=granted,
                      reason_code=reason, p_kw_setpoint=-3.0),
        cycle_id=f"cycle-{cycle}",
        items=[ProposedItem("hub-" + bank_id, -3.0, reason, obligation_id, granted)],
    )  # fmt: skip
    wire_default_passing_scenario(fakes, proposal)
    fakes.commitments.active_by_bank.pop(bank_id, None)  # the per-bank _Commitments fake is the read
    return await service.evaluate_and_sign(make_batch_row(proposal))


async def test_a_partial_ecrs_deployment_on_three_banks_passes_g19_every_cycle(
    fakes, guardian_config, signing_seed
):
    """3 banks, unequal shares of a 0.3 MW deployment of a 0.5 MW award; the engine persists each cycle's grants
    before the verdict. Every bank's batch passes G-19 on every cycle, from the hold (prior 0) onwards."""
    reserved = {"bank-027": Decimal("200.4"), "bank-034": Decimal("150.1"), "bank-035": Decimal("149.5")}
    shares = {"bank-027": Decimal("120.24"), "bank-034": Decimal("90.06"), "bank-035": Decimal("89.70")}
    service, obligation_id = _world(fakes, guardian_config, signing_seed, reserved)
    for bank in reserved:
        fakes.prior_grants.by_bank[(obligation_id, bank)] = Decimal(0)  # held at 0 kW before the call
    seq = 0
    for cycle in range(1, 11):
        for bank, share in shares.items():
            seq += 1
            verdict = await _judge(fakes, service, obligation_id, bank, share, cycle, seq)
            assert "G-19" not in verdict.vetoed_rule_ids, (cycle, bank, fakes.trace.appended[-1][1])
            assert verdict.outcome == "PASS", (cycle, bank, verdict.vetoed_rule_ids)
        for bank, share in shares.items():  # this cycle's grants become each bank's own prior
            fakes.prior_grants.by_bank[(obligation_id, bank)] = share


async def test_the_live_failure_the_obligation_wide_prior_vetoed_the_smaller_share(
    fakes, guardian_config, signing_seed
):
    """Reproduces 046a2ebb: with the other bank's 150.424 kW as the "prior", bank-034's 149.576 kW is vetoed."""
    reserved = {"bank-027": Decimal("250.707"), "bank-034": Decimal("249.293")}
    service, obligation_id = _world(fakes, guardian_config, signing_seed, reserved)
    # the pre-fix guardian exactly: an obligation-wide prior and no deployed-share lock (each fix alone clears it)
    service.ports = replace(
        service.ports, prior_grants=_ObligationWidePrior(Decimal("150.424")), as_awards=None
    )
    verdict = await _judge(fakes, service, obligation_id, "bank-034", Decimal("149.576"), 1, 1)
    assert "G-19" in verdict.vetoed_rule_ids  # the old read; the fixed port scopes it to bank-034 itself

    service.ports = replace(service.ports, prior_grants=fakes.prior_grants)
    fakes.prior_grants.by_bank[(obligation_id, "bank-034")] = Decimal("149.576")
    verdict = await _judge(fakes, service, obligation_id, "bank-034", Decimal("149.576"), 2, 2)
    assert verdict.outcome == "PASS"  # the per-bank prior alone


async def test_a_bank_joining_mid_deployment_is_held_to_its_share_of_the_request(
    fakes, guardian_config, signing_seed
):
    """No prior grant on this bank yet (the interval moved the reservation to bank-035): the lock is its share of
    the 0.3 MW deployed, not its whole 0.5 MW reservation -- and under-delivering that share is still vetoed."""
    service, obligation_id = _world(fakes, guardian_config, signing_seed, {"bank-035": AWARD_KW})
    assert (await _judge(fakes, service, obligation_id, "bank-035", Decimal("300"), 1, 1)).outcome == "PASS"
    verdict = await _judge(fakes, service, obligation_id, "bank-035", Decimal("250"), 2, 2)
    assert "G-19" in verdict.vetoed_rule_ids


@pytest.mark.parametrize(
    ("service_type", "requested"),
    [("ERCOT_AS", None), ("DIST_DEFERRAL", REQUESTED_KW)],
    ids=["full-deployment", "not-a-hold"],
)
async def test_the_full_reservation_is_the_lock_otherwise(
    fakes, guardian_config, signing_seed, service_type, requested
):
    service, obligation_id = _world(
        fakes,
        guardian_config,
        signing_seed,
        {"bank-035": AWARD_KW},
        service_type=service_type,
        requested=requested,
    )
    verdict = await _judge(fakes, service, obligation_id, "bank-035", Decimal("300"), 1, 1)
    assert "G-19" in verdict.vetoed_rule_ids


async def test_the_prior_grant_read_is_this_banks_previous_cycle():
    cursor = FakeCursor([(Decimal("149.576"),)])
    port = repo.PgPriorGrantPort(FakePool(cursor))
    obligation_id = uuid4()
    assert await port.prior_granted_kw(obligation_id, "bank-034", "cycle-9") == Decimal("149.576")
    sql, params = cursor.executed[0]
    assert "bank_id::text = %(bank_id)s" in sql and "cycle_id <> %(cycle_id)s" in sql
    assert params == {"obligation_id": obligation_id, "bank_id": "bank-034", "cycle_id": "cycle-9"}


# --- the lead's regression set (r3.4.4 live, toll-critical) ------------------------------------------------------

LIVE_RESERVED = {"bank-027": Decimal("250.707"), "bank-034": Decimal("249.293")}
LIVE_SHARES = {"bank-027": Decimal("150.424"), "bank-034": Decimal("149.576")}


@pytest.mark.parametrize("reason", ["R-GRANT-COMMITTED", "R-AS-PARTIAL-DEPLOYMENT"])
async def test_the_live_two_bank_partial_ecrs_passes_every_cycle(
    fakes, guardian_config, signing_seed, reason
):
    """300 of 500 kW on bank-027/bank-034 with exactly the live 150.424/149.576 split, untagged or tagged."""
    service, obligation_id = _world(fakes, guardian_config, signing_seed, LIVE_RESERVED)
    for bank in LIVE_RESERVED:
        fakes.prior_grants.by_bank[(obligation_id, bank)] = Decimal(0)
    seq = 0
    for cycle in range(1, 21):
        for bank, share in LIVE_SHARES.items():
            seq += 1
            verdict = await _judge(fakes, service, obligation_id, bank, share, cycle, seq, reason)
            assert verdict.outcome == "PASS", (cycle, bank, verdict.vetoed_rule_ids)
        for bank, share in LIVE_SHARES.items():
            fakes.prior_grants.by_bank[(obligation_id, bank)] = share


@pytest.mark.parametrize("reason", ["R-GRANT-COMMITTED", "R-AS-PARTIAL-DEPLOYMENT"])
async def test_a_real_under_delivery_of_the_partial_call_is_still_vetoed(
    fakes, guardian_config, signing_seed, reason
):
    service, obligation_id = _world(fakes, guardian_config, signing_seed, LIVE_RESERVED)
    fakes.prior_grants.by_bank[(obligation_id, "bank-034")] = LIVE_SHARES["bank-034"]
    verdict = await _judge(fakes, service, obligation_id, "bank-034", Decimal("120"), 2, 1, reason)
    assert "G-19" in verdict.vetoed_rule_ids


@pytest.mark.parametrize("reason", ["R-GRANT-COMMITTED", "R-AS-PARTIAL-DEPLOYMENT"])
async def test_a_partial_toll_call_on_the_single_substation_bank_passes(
    fakes, guardian_config, signing_seed, reason
):
    """The 16:45 AE sim call: -20 MW against the toll's larger commitment, on its one substation bank, from the
    0 kW hold (and also with no prior grant on the bank at all)."""
    reserved = {"bank-sub-LZ_AEN-00": Decimal("20408")}
    service, obligation_id = _world(
        fakes,
        guardian_config,
        signing_seed,
        reserved,
        service_type="REGULATED_CAPACITY",
        requested=Decimal("20000"),
    )
    verdict = await _judge(
        fakes, service, obligation_id, "bank-sub-LZ_AEN-00", Decimal("20000"), 1, 1, reason
    )
    assert verdict.outcome == "PASS", verdict.vetoed_rule_ids
    fakes.prior_grants.by_bank[(obligation_id, "bank-sub-LZ_AEN-00")] = Decimal(0)
    verdict = await _judge(
        fakes, service, obligation_id, "bank-sub-LZ_AEN-00", Decimal("20000"), 2, 2, reason
    )
    assert verdict.outcome == "PASS", verdict.vetoed_rule_ids


async def test_the_partial_tag_without_a_deployment_is_vetoed(fakes, guardian_config, signing_seed):
    service, obligation_id = _world(fakes, guardian_config, signing_seed, LIVE_RESERVED, deployed=False)
    verdict = await _judge(
        fakes, service, obligation_id, "bank-034", LIVE_SHARES["bank-034"], 1, 1, "R-AS-PARTIAL-DEPLOYMENT"
    )
    assert "G-19" in verdict.vetoed_rule_ids
    assert {v["reason"] for v in fakes.trace.appended[-1][1]["violations"]} == {
        "AS_PARTIAL_DEPLOYMENT_UNVERIFIED"
    }


async def test_the_share_allows_the_grant_tables_rounding(fakes, guardian_config, signing_seed):
    """og.grant keeps 3 decimals: a share rounded down by 0.0005 kW is not a lock dip."""
    service, obligation_id = _world(fakes, guardian_config, signing_seed, {"bank-035": AWARD_KW})
    verdict = await _judge(fakes, service, obligation_id, "bank-035", Decimal("299.9995"), 1, 1)
    assert verdict.outcome == "PASS"
