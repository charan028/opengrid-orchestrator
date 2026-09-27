"""ERCOT awards and dispatch instructions -> the orchestrator's existing intake (protocol-adapters.md S6.5).

- An energy or AS AWARD becomes an OFFERED opportunity on the contract that represents that resource's
  market participation (a `FREE`-market ERCOT_ENERGY / ERCOT_AS contract), through
  `opengrid.contracts.admit_priced` -- the same admission path every other opportunity takes, so product
  rules, activation gates and the commitment lock (K13) apply unchanged. The award price is recorded
  as `value_per_mwh`.
- An AS deployment INSTRUCTION becomes an `og.as_deployment` row (migration 0020), which is what already
  releases held ERCOT_AS awards for discharge. The row's `source` must be one the CHECK allows: the
  simulator path uses `MARKET_SIM`; a real-ERCOT source value needs an additive migration at go-live.

Nothing here writes to the database: `apply_awards` takes the admission callable, and
`deployments_for` returns rows for the caller (the operator API store) to insert.
"""

from __future__ import annotations

import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, timedelta
from decimal import Decimal
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict

from opengrid.integrations.interfaces import Award, DispatchInstruction

logger = logging.getLogger(__name__)

__all__ = [
    "AsDeploymentRequest",
    "AwardContractMap",
    "AwardIntake",
    "apply_awards",
    "awards_to_intake",
    "deployments_for",
]


class AwardContractMap(BaseModel):
    """`[integrations.market.contracts]`: which contract carries each resource's awards.
    Keys: `energy["<resource>"]`, `ancillary["<resource>:<SERVICE>"]`."""

    model_config = ConfigDict(extra="forbid", frozen=True)

    energy: dict[str, UUID] = {}
    ancillary: dict[str, UUID] = {}

    def contract_for(self, award: Award) -> UUID | None:
        if award.kind == "ENERGY":
            return self.energy.get(award.resource_id)
        return self.contract_for_as(award.resource_id, award.service)

    def contract_for_as(self, resource_id: str, service: str | None) -> UUID | None:
        """The contract carrying `resource_id`'s awards of AS `service` (also how an AS deployment
        instruction, which names only the resource and AS type, finds its award -- D-35)."""
        return self.ancillary.get(f"{resource_id}:{service}")


@dataclass(frozen=True, slots=True)
class AwardIntake:
    award_id: str
    contract_id: UUID
    window_start: datetime
    window_end: datetime
    requested_kw: Decimal
    value_per_mwh: Decimal | None


def awards_to_intake(
    awards: list[Award], contracts: AwardContractMap
) -> tuple[list[AwardIntake], list[Award]]:
    """(intakes, skipped). Skipped: no mapped contract, zero MW, or an ESR CHARGING award (negative MW:
    a charge schedule, not a sale -- it is planned by the charging model, not admitted as an obligation)."""
    intakes: list[AwardIntake] = []
    skipped: list[Award] = []
    for award in awards:
        contract_id = contracts.contract_for(award)
        if contract_id is None or award.awarded_mw <= 0:
            skipped.append(award)
            continue
        intakes.append(
            AwardIntake(
                award_id=award.award_id,
                contract_id=contract_id,
                window_start=award.interval_start,
                window_end=award.interval_end,
                requested_kw=(award.awarded_mw * 1000).quantize(Decimal("0.1")),
                value_per_mwh=award.price_usd_per_mwh,
            )
        )
    return intakes, skipped


AdmitPriced = Callable[..., Awaitable[object]]


async def apply_awards(
    awards: list[Award], contracts: AwardContractMap, admit_priced: AdmitPriced
) -> list[tuple[AwardIntake, object | Exception]]:
    """Admit every mappable award through `admit_priced(contract_id, start, end, kw, value_per_mwh=...)`
    (wire `opengrid.contracts.admit_priced`). One award's rejection never blocks the others; each result
    is returned (the Opportunity, or the exception, e.g. `AdmissionError`) for tracing."""
    intakes, skipped = awards_to_intake(awards, contracts)
    for award in skipped:
        logger.warning(
            "ERCOT award not admitted", extra={"award_id": award.award_id, "mw": str(award.awarded_mw)}
        )
    results: list[tuple[AwardIntake, object | Exception]] = []
    for intake in intakes:
        try:
            result = await admit_priced(
                intake.contract_id,
                intake.window_start,
                intake.window_end,
                intake.requested_kw,
                value_per_mwh=intake.value_per_mwh,
            )
        except Exception as exc:  # AdmissionError and friends: reported, never fatal to the batch
            result = exc
        results.append((intake, result))
    return results


@dataclass(frozen=True, slots=True)
class AsDeploymentRequest:
    """One `og.as_deployment` row to insert (`obligation_id` NULL deploys every ERCOT_AS award)."""

    obligation_id: UUID | None
    start_at: datetime
    end_at: datetime
    source: Literal["MARKET_SIM"]
    requested_by: str
    reason: str


def deployments_for(
    instructions: list[DispatchInstruction], *, default_duration: timedelta = timedelta(minutes=15)
) -> list[AsDeploymentRequest]:
    """AS_DEPLOYMENT instructions -> deployment rows. Recalls and plain VDIs are not deployments (a recall
    ends a deployment early: the caller cancels the matching row via `cancel_as_deployment`)."""
    rows: list[AsDeploymentRequest] = []
    for instruction in instructions:
        if instruction.kind != "AS_DEPLOYMENT":
            continue
        end = instruction.end_at or instruction.start_at + default_duration
        if end <= instruction.start_at:
            continue
        rows.append(
            AsDeploymentRequest(
                obligation_id=None,
                start_at=instruction.start_at,
                end_at=end,
                source="MARKET_SIM",
                requested_by="ERCOT_MMS",
                reason=f"ERCOT {instruction.service or 'AS'} deployment {instruction.instruction_id}"
                + (f" ({instruction.mw} MW)" if instruction.mw is not None else ""),
            )
        )
    return rows
