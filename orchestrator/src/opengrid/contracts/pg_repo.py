"""Postgres-backed `ContractsRepo` (02a S1: `og.contract`, `og.product_rule`, `og.opportunity`,
`og.obligation`, `og.renomination_point`). Kept separate from the pure admission/lifecycle/
re-nomination logic so those stay unit-testable without a database (BUILD.md S5a "pure logic
separated from I/O"), mirroring `opengrid.trace`'s `store.py`/`pg_backend.py` split.

All statements are parameterised (BUILD.md S5a: "parameterised SQL only"); every call runs through
`opengrid.platform.db`'s pool, never a bare `psycopg.connect`.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any
from uuid import UUID

from psycopg.rows import dict_row
from psycopg_pool import AsyncConnectionPool

from opengrid.contracts.errors import ConcurrentUpdateError
from opengrid.core.models.engine import (
    Contract,
    Obligation,
    ObligationState,
    Opportunity,
    ProductRule,
    RenominationPoint,
)


def _contract_from_row(row: dict[str, Any]) -> Contract:
    return Contract(
        contract_id=row["contract_id"],
        customer_id=row["customer_id"],
        service_type=row["service_type"],
        variant=row["variant"],
        tier=row["tier"],
        profile_ref=row["profile_ref"],
        territory_id=row["territory_id"],
        start_at=row["start_at"],
        end_at=row["end_at"],
        renomination_allowed=row["renomination_allowed"],
        penalty_alpha=row["penalty_alpha"],
        penalty_beta=row["penalty_beta"],
        penalty_theta=row["penalty_theta"],
        degradation_cost=row["degradation_cost"],
        fallback_allowed=row["fallback_allowed"],
        status=row["status"],
        # Migration 0025 (two-market model). `.get`: a row read from a query that does not select them, or
        # a database before 0025, is FREE with no utility -- the column default, never a guess.
        market=row.get("market") or "FREE",
        utility_id=row.get("utility_id"),
    )


def _product_rule_from_row(row: dict[str, Any]) -> ProductRule:
    return ProductRule(
        product_rule_id=row["product_rule_id"],
        contract_id=row["contract_id"],
        product_code=row["product_code"],
        min_qty_kw=row["min_qty_kw"],
        increment_kw=row["increment_kw"],
        block=row["block"],
        duration_minutes=row["duration_minutes"],
        variable_kind=row["variable_kind"],
    )


def _opportunity_from_row(row: dict[str, Any]) -> Opportunity:
    return Opportunity(
        opportunity_id=row["opportunity_id"],
        contract_id=row["contract_id"],
        product_rule_id=row["product_rule_id"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        requested_kw=row["requested_kw"],
        value_per_mwh=row["value_per_mwh"],
        scenario_basis=row["scenario_basis"],
        state=row["state"],
        reason_code=row["reason_code"],
        admitted_at=row["admitted_at"],
        decided_at=row["decided_at"],
        gate_id=row["gate_id"],
    )


def _obligation_from_row(row: dict[str, Any]) -> Obligation:
    return Obligation(
        obligation_id=row["obligation_id"],
        opportunity_id=row["opportunity_id"],
        contract_id=row["contract_id"],
        service_type=row["service_type"],
        tier=row["tier"],
        window_start=row["window_start"],
        window_end=row["window_end"],
        committed_qty_kw=row["committed_qty_kw"],
        state=row["state"],
        at_risk=row["at_risk"],
        last_reason_code=row["last_reason_code"],
        version=row["version"],
    )


def _renomination_point_from_row(row: dict[str, Any]) -> RenominationPoint:
    return RenominationPoint(
        renomination_point_id=row["renomination_point_id"],
        contract_id=row["contract_id"],
        obligation_id=row["obligation_id"],
        scheduled_at=row["scheduled_at"],
        exercised_at=row["exercised_at"],
        outcome=row["outcome"],
        plan_id=row["plan_id"],
    )


class PgContractsRepo:
    """`ContractsRepo` implementation over an `AsyncConnectionPool` (`opengrid.platform.db.make_pool`)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    # -- contract / product_rule ------------------------------------------------------------------

    async def get_contract(self, contract_id: UUID) -> Contract | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM og.contract WHERE contract_id = %s", (contract_id,))
            row = await cur.fetchone()
            return _contract_from_row(row) if row else None

    async def list_contracts(
        self, *, customer_id: UUID | None = None, service_type: str | None = None, status: str | None = None
    ) -> list[Contract]:
        clauses: list[str] = []
        params: list[Any] = []
        if customer_id is not None:
            clauses.append("customer_id = %s")
            params.append(customer_id)
        if service_type is not None:
            clauses.append("service_type = %s")
            params.append(service_type)
        if status is not None:
            clauses.append("status = %s")
            params.append(status)
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(f"SELECT * FROM og.contract {where} ORDER BY created_at", params)  # noqa: S608
            return [_contract_from_row(r) for r in await cur.fetchall()]

    async def list_customer_ids(self) -> list[UUID]:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT DISTINCT customer_id FROM og.contract ORDER BY customer_id")
            return [r[0] for r in await cur.fetchall()]

    async def upsert_contract(self, contract: Contract) -> Contract:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO og.contract (
                    contract_id, customer_id, service_type, variant, tier, profile_ref, territory_id,
                    start_at, end_at, renomination_allowed, penalty_alpha, penalty_beta, penalty_theta,
                    degradation_cost, fallback_allowed, status, market, utility_id, updated_at
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
                ON CONFLICT (contract_id) DO UPDATE SET
                    customer_id = EXCLUDED.customer_id, service_type = EXCLUDED.service_type,
                    variant = EXCLUDED.variant, tier = EXCLUDED.tier, profile_ref = EXCLUDED.profile_ref,
                    territory_id = EXCLUDED.territory_id, start_at = EXCLUDED.start_at,
                    end_at = EXCLUDED.end_at, renomination_allowed = EXCLUDED.renomination_allowed,
                    penalty_alpha = EXCLUDED.penalty_alpha, penalty_beta = EXCLUDED.penalty_beta,
                    penalty_theta = EXCLUDED.penalty_theta, degradation_cost = EXCLUDED.degradation_cost,
                    fallback_allowed = EXCLUDED.fallback_allowed, status = EXCLUDED.status,
                    market = EXCLUDED.market, utility_id = EXCLUDED.utility_id,
                    updated_at = now()
                RETURNING *
                """,
                (
                    contract.contract_id,
                    contract.customer_id,
                    contract.service_type,
                    contract.variant,
                    contract.tier,
                    contract.profile_ref,
                    contract.territory_id,
                    contract.start_at,
                    contract.end_at,
                    contract.renomination_allowed,
                    contract.penalty_alpha,
                    contract.penalty_beta,
                    contract.penalty_theta,
                    contract.degradation_cost,
                    contract.fallback_allowed,
                    contract.status,
                    contract.market,
                    contract.utility_id,
                ),
            )
            row = await cur.fetchone()
            await conn.commit()
            if row is None:
                raise RuntimeError(f"upsert of contract {contract.contract_id} returned no row")
            return _contract_from_row(row)

    async def get_product_rules(self, contract_id: UUID) -> list[ProductRule]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM og.product_rule WHERE contract_id = %s ORDER BY product_code", (contract_id,)
            )
            return [_product_rule_from_row(r) for r in await cur.fetchall()]

    async def upsert_product_rule(self, rule: ProductRule) -> ProductRule:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                INSERT INTO og.product_rule (
                    product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block,
                    duration_minutes, variable_kind
                ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s)
                ON CONFLICT (contract_id, product_code) DO UPDATE SET
                    min_qty_kw = EXCLUDED.min_qty_kw, increment_kw = EXCLUDED.increment_kw,
                    block = EXCLUDED.block, duration_minutes = EXCLUDED.duration_minutes,
                    variable_kind = EXCLUDED.variable_kind
                RETURNING *
                """,
                (
                    rule.product_rule_id,
                    rule.contract_id,
                    rule.product_code,
                    rule.min_qty_kw,
                    rule.increment_kw,
                    rule.block,
                    rule.duration_minutes,
                    rule.variable_kind,
                ),
            )
            row = await cur.fetchone()
            await conn.commit()
            if row is None:
                raise RuntimeError(f"upsert of product_rule {rule.product_rule_id} returned no row")
            return _product_rule_from_row(row)

    # -- opportunity / obligation ------------------------------------------------------------------

    async def create_opportunity_and_obligation(
        self, opportunity: Opportunity, obligation: Obligation
    ) -> None:
        async with self._pool.connection() as conn:
            async with conn.cursor() as cur:
                await cur.execute(
                    """
                    INSERT INTO og.opportunity (
                        opportunity_id, contract_id, product_rule_id, window_start, window_end,
                        requested_kw, value_per_mwh, scenario_basis, state, admitted_at
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        opportunity.opportunity_id,
                        opportunity.contract_id,
                        opportunity.product_rule_id,
                        opportunity.window_start,
                        opportunity.window_end,
                        opportunity.requested_kw,
                        opportunity.value_per_mwh,
                        opportunity.scenario_basis,
                        opportunity.state,
                        opportunity.admitted_at,
                    ),
                )
                await cur.execute(
                    """
                    INSERT INTO og.obligation (
                        obligation_id, opportunity_id, contract_id, service_type, tier,
                        window_start, window_end, committed_qty_kw, state
                    ) VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s)
                    """,
                    (
                        obligation.obligation_id,
                        obligation.opportunity_id,
                        obligation.contract_id,
                        obligation.service_type,
                        obligation.tier,
                        obligation.window_start,
                        obligation.window_end,
                        obligation.committed_qty_kw,
                        obligation.state,
                    ),
                )
            await conn.commit()

    async def get_opportunity(self, opportunity_id: UUID) -> Opportunity | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM og.opportunity WHERE opportunity_id = %s", (opportunity_id,))
            row = await cur.fetchone()
            return _opportunity_from_row(row) if row else None

    async def update_opportunity_state(
        self,
        opportunity_id: UUID,
        *,
        state: str,
        reason_code: str | None,
        decided_at: datetime,
        gate_id: UUID | None = None,
    ) -> Opportunity:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE og.opportunity
                SET state = %s, reason_code = %s, decided_at = %s, gate_id = COALESCE(%s, gate_id)
                WHERE opportunity_id = %s
                RETURNING *
                """,
                (state, reason_code, decided_at, gate_id, opportunity_id),
            )
            row = await cur.fetchone()
            await conn.commit()
            if row is None:
                raise LookupError(f"no such opportunity: {opportunity_id}")
            return _opportunity_from_row(row)

    async def get_obligation(self, obligation_id: UUID) -> Obligation | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM og.obligation WHERE obligation_id = %s", (obligation_id,))
            row = await cur.fetchone()
            return _obligation_from_row(row) if row else None

    async def get_obligation_by_opportunity(self, opportunity_id: UUID) -> Obligation | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute("SELECT * FROM og.obligation WHERE opportunity_id = %s", (opportunity_id,))
            row = await cur.fetchone()
            return _obligation_from_row(row) if row else None

    async def update_obligation_state(
        self,
        obligation_id: UUID,
        *,
        to_state: ObligationState,
        reason_code: str | None,
        expected_version: int,
        at_risk: bool | None = None,
    ) -> Obligation:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE og.obligation
                SET state = %s, last_reason_code = %s, version = version + 1, updated_at = now(),
                    at_risk = COALESCE(%s, at_risk)
                WHERE obligation_id = %s AND version = %s
                RETURNING *
                """,
                (to_state, reason_code, at_risk, obligation_id, expected_version),
            )
            row = await cur.fetchone()
            await conn.commit()
            if row is None:
                existing = await self.get_obligation(obligation_id)
                if existing is None:
                    raise LookupError(f"no such obligation: {obligation_id}")
                raise ConcurrentUpdateError(obligation_id, expected_version)
            return _obligation_from_row(row)

    async def set_obligation_at_risk(self, obligation_id: UUID, at_risk: bool) -> Obligation:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "UPDATE og.obligation SET at_risk = %s, updated_at = now() WHERE obligation_id = %s RETURNING *",
                (at_risk, obligation_id),
            )
            row = await cur.fetchone()
            await conn.commit()
        if row is None:
            raise LookupError(f"no such obligation: {obligation_id}")
        return _obligation_from_row(row)

    async def active_obligations_by_interval(
        self,
        interval_start: datetime,
        interval_end: datetime,
        *,
        service_type: str | None = None,
        states: tuple[ObligationState, ...] = ("COMMITTED", "DELIVERING"),
    ) -> list[Obligation]:
        params: list[Any] = [interval_end, interval_start, list(states)]
        service_clause = ""
        if service_type is not None:
            service_clause = "AND service_type = %s"
            params.append(service_type)
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                f"""
                SELECT * FROM og.obligation
                WHERE window_start < %s AND window_end > %s AND state = ANY(%s) {service_clause}
                ORDER BY window_start
                """,  # noqa: S608 -- service_clause is a fixed literal, never interpolated user input
                params,
            )
            return [_obligation_from_row(r) for r in await cur.fetchall()]

    async def unselected_offered_before(self, cutoff: datetime) -> list[Opportunity]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM og.opportunity WHERE state = 'OFFERED' AND window_start <= %s", (cutoff,)
            )
            return [_opportunity_from_row(r) for r in await cur.fetchall()]

    async def find_opportunity_by_window(
        self, contract_id: UUID, window_start: datetime, window_end: datetime
    ) -> Opportunity | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM og.opportunity
                WHERE contract_id = %s AND window_start = %s AND window_end = %s
                LIMIT 1
                """,
                (contract_id, window_start, window_end),
            )
            row = await cur.fetchone()
            return _opportunity_from_row(row) if row else None

    # -- renomination_point -------------------------------------------------------------------------

    async def due_renomination_points(self, as_of: datetime) -> list[RenominationPoint]:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                SELECT * FROM og.renomination_point
                WHERE scheduled_at <= %s AND exercised_at IS NULL
                ORDER BY scheduled_at
                """,
                (as_of,),
            )
            return [_renomination_point_from_row(r) for r in await cur.fetchall()]

    async def get_renomination_point(self, renomination_point_id: UUID) -> RenominationPoint | None:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                "SELECT * FROM og.renomination_point WHERE renomination_point_id = %s",
                (renomination_point_id,),
            )
            row = await cur.fetchone()
            return _renomination_point_from_row(row) if row else None

    async def mark_renomination_exercised(
        self,
        renomination_point_id: UUID,
        *,
        outcome: str,
        plan_id: UUID | None,
        exercised_at: datetime,
    ) -> RenominationPoint:
        async with self._pool.connection() as conn, conn.cursor(row_factory=dict_row) as cur:
            await cur.execute(
                """
                UPDATE og.renomination_point SET outcome = %s, plan_id = %s, exercised_at = %s
                WHERE renomination_point_id = %s
                RETURNING *
                """,
                (outcome, plan_id, exercised_at, renomination_point_id),
            )
            row = await cur.fetchone()
            await conn.commit()
            if row is None:
                raise LookupError(f"no such renomination_point: {renomination_point_id}")
            return _renomination_point_from_row(row)
