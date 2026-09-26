"""Concrete `opengrid.allocator.gateways` adapters wiring the allocator's 2 s cycle to
`opengrid.fleet`/`opengrid.ledger`/`og.feed_obs` (dispatch-live pass, merge task item 2).

Why this lives in `opengrid.engine`: `opengrid.allocator`'s own docstring (`allocator/gateways.py`) is
explicit that "production wiring (owned by `engine`, 02b S1.2) supplies real gateways" -- the allocator's
pure cycle logic and its thin I/O adapter (`run_cycle`) must never import `opengrid.fleet`'s Postgres
backend or hold a connection pool themselves (BUILD.md S5a "pure logic separated from I/O"). This module
is that engine-owned wiring layer, called once per 2 s tick from `opengrid.engine._engine_tick`.

Price/schedule data is read directly from `og.feed_obs` here rather than through `opengrid.feeds.latest`
because `opengrid.feeds` is a *separate process* (`og-feeds`) with its own private `FeedStore` singleton
-- `opengrid.feeds.latest()` would raise `RuntimeError` if called from `og-engine`'s process, since
nothing ever calls `opengrid.feeds.run_feeds_process()` there. Reading the same table both processes
share is the correct cross-process boundary (mirrors `opengrid.guardian.repo.PgBankStatePort` reading
`og.feed_obs` directly for the identical reason).
"""

from __future__ import annotations

import logging
from collections.abc import Sequence
from datetime import datetime
from decimal import Decimal
from uuid import NAMESPACE_URL, UUID, uuid5

from psycopg_pool import AsyncConnectionPool

from opengrid import fleet, ledger
from opengrid.allocator.models import (
    BankSnapshot,
    FleetState,
    HubSnapshot,
    Instruction,
    LedgerView,
    ObligationCall,
    PriceSignal,
    ProposedGrant,
    ScadaSample,
    Schedule,
)
from opengrid.core.models.mqtt import ScadaUtilityInstruction
from opengrid.ledger import GrantRecord

logger = logging.getLogger(__name__)

# 02b's "current price" signal for the allocator's headroom/dwell threshold (S6) -- ERCOT settlement
# point price (np6-905-cd, see opengrid.feeds.ercot's product map), the same product
# opengrid.api/opengrid.guardian already treat as the live price series. Not a hub-specific LMP -- MVP-S
# uses one system-wide price for the $/MWh threshold, per 02a S5.5's single hysteresis/hub.
_PRICE_PRODUCT = "np6-905-cd"

_LATEST_PRICE_SQL = """
SELECT value FROM og.feed_obs WHERE source = 'ERCOT' AND product = %(product)s
ORDER BY ts DESC LIMIT 1
"""

# Active (COMMITTED/DELIVERING) obligations' calls on the given banks for "now" (02a S1's active_calls):
# the reservation is the K13 frozen floor; obligation/opportunity give service_type/tier/value.
_ACTIVE_CALLS_SQL = """
SELECT r.obligation_id, r.bank_id, r.amount, o.service_type, o.tier, op.value_per_mwh
FROM og.reservation r
JOIN og.obligation o ON o.obligation_id = r.obligation_id
JOIN og.opportunity op ON op.opportunity_id = o.opportunity_id
WHERE r.bank_id = ANY(%(bank_ids)s)
  AND r.released_at IS NULL
  AND r.interval_start <= %(now)s AND r.interval_end > %(now)s
  AND o.state IN ('COMMITTED', 'DELIVERING')
"""

# Latest grant per obligation on these banks -- `prior_granted_kw` (02a S5.1's K13 "never below
# min(committed, prior_granted)" floor). DISTINCT ON picks the most recent row per obligation.
_PRIOR_GRANTS_SQL = """
SELECT DISTINCT ON (obligation_id) obligation_id, granted_kw
FROM og.grant
WHERE bank_id = ANY(%(bank_ids)s) AND obligation_id IS NOT NULL
ORDER BY obligation_id, created_at DESC
"""

_HEALTH_TO_ALLOCATOR: dict[str, str] = {
    "online": "OK",
    "stale": "LAGGING",
    "offline": "STALE",
    "fault": "FAULT",
}


class FleetCapabilityProvider:
    """`opengrid.ledger.CapabilityProvider` backed by the fleet twin -- wires `ReservationLedger`'s K2
    one-buyer check to the same `fleet.capability()` the allocator/selector already read (merge task:
    `opengrid.ledger.configure()` was never called anywhere, so every `selector.run_gate` ->
    `ledger.reserve()` call raised `RuntimeError` before this)."""

    async def capability_kw(self, bank_id: str, interval_start: datetime) -> Decimal:
        cap = await fleet.capability(bank_id, interval_start)
        return Decimal(str(cap.max_discharge_kw))


class EngineFleetGateway:
    """`opengrid.allocator.gateways.FleetGateway` backed by the in-process fleet twin
    (`opengrid.fleet`, already loaded/kept warm by `og-engine`'s own `load_topology()`/MQTT ingest --
    no extra I/O here beyond what the twin already does)."""

    async def bank_ids(self) -> Sequence[str]:
        return fleet.known_bank_ids()

    async def fleet_state(self, bank_ids: Sequence[str], interval_start: datetime) -> FleetState:
        hubs: list[HubSnapshot] = []
        banks: list[BankSnapshot] = []
        for bank_id in bank_ids:
            try:
                cap = await fleet.capability(bank_id, interval_start)
            except LookupError:
                logger.warning("fleet_state: unknown bank_id, skipping", extra={"bank_id": bank_id})
                continue
            scada = fleet.bank_scada_signal(bank_id)
            load_kva = scada.value if scada is not None and scada.signal == "APPARENT_POWER_KVA" else 0.0
            banks.append(
                BankSnapshot(
                    bank_id=bank_id,
                    capability_kw=cap.max_discharge_kw,
                    load_kva=load_kva,
                    zone=fleet.bank_zone(bank_id),
                )
            )
            for snap in fleet.hub_capabilities(bank_id):
                hubs.append(
                    HubSnapshot(
                        hub_id=snap.hub_id,
                        bank_id=snap.bank_id,
                        free_discharge_kw=snap.free_discharge_kw,
                        health=_HEALTH_TO_ALLOCATOR.get(snap.health, "FAULT"),  # type: ignore[arg-type]
                    )
                )
        return FleetState(hubs=tuple(hubs), banks=tuple(banks))


class EngineScadaGateway:
    """`ScadaGateway` backed by `opengrid.fleet`'s stored latest SCADA readings (already ingested off
    MQTT by `og-engine`'s own subscriber, `opengrid.engine._mqtt_ingest_loop`)."""

    async def samples(self, bank_ids: Sequence[str]) -> dict[str, ScadaSample]:
        samples: dict[str, ScadaSample] = {}
        for bank_id in bank_ids:
            signal = fleet.bank_scada_signal(bank_id)
            if signal is None or signal.signal != "APPARENT_POWER_KVA":
                continue
            samples[bank_id] = ScadaSample(bank_id=bank_id, apparent_power_kva=signal.value)
        return samples


class EngineScheduleGateway:
    """`ScheduleGateway` reading the live price directly from `og.feed_obs` (see module docstring for
    why this doesn't go through `opengrid.feeds.latest`), and L2 instructions from the fleet twin's own
    ingest (`opengrid.fleet.utility_instruction`, already validated/stored off MQTT)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def schedule(self, bank_ids: Sequence[str]) -> Schedule:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_LATEST_PRICE_SQL, {"product": _PRICE_PRODUCT})
            row = await cur.fetchone()
        if row is None:
            logger.warning("no live price observed yet; allocator sees price=0.0 this cycle")
            price = 0.0
        else:
            price = float(row[0])
        return Schedule(prices=tuple(PriceSignal(bank_id=b, price_usd_per_mwh=price) for b in bank_ids))

    async def instructions(self, bank_ids: Sequence[str]) -> Sequence[Instruction]:
        instructions: list[Instruction] = []
        for bank_id in bank_ids:
            instr: ScadaUtilityInstruction | None = fleet.utility_instruction(bank_id)
            if instr is None:
                continue
            instructions.append(
                Instruction(scope="BANK", scope_ref=bank_id, kind=instr.kind, limit_kw=instr.limit_kw)
            )
        return tuple(instructions)


class EngineLedgerGateway:
    """`LedgerGateway` backed by `og.reservation`/`og.obligation`/`og.opportunity` reads (this module's
    own SQL -- `opengrid.ledger`'s public interface is reservation-write-focused, not obligation-state
    read-focused, so a plain read query here does not duplicate any of its logic) plus
    `opengrid.ledger.persist_grants`/`ledger_version` for the writes (BUILD.md S1 "no duplicated
    functions": the actual grant-persistence logic lives in exactly one place, `opengrid.ledger`)."""

    def __init__(self, pool: AsyncConnectionPool) -> None:
        self._pool = pool

    async def ledger_view(self, bank_ids: Sequence[str], interval_start: datetime) -> LedgerView:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_ACTIVE_CALLS_SQL, {"bank_ids": list(bank_ids), "now": interval_start})
            call_rows = await cur.fetchall()
            await cur.execute(_PRIOR_GRANTS_SQL, {"bank_ids": list(bank_ids)})
            prior_rows = await cur.fetchall()

        prior_by_obligation = {str(obligation_id): float(kw) for obligation_id, kw in prior_rows}

        calls: list[ObligationCall] = []
        for obligation_id, bank_id, amount, service_type, tier, value_per_mwh in call_rows:
            try:
                eligible_hub_ids = tuple(
                    s.hub_id for s in fleet.hub_capabilities(bank_id) if s.health == "online"
                )
            except LookupError:
                eligible_hub_ids = ()
            calls.append(
                ObligationCall(
                    obligation_id=str(obligation_id),
                    bank_id=bank_id,
                    service_type=service_type,
                    tier=tier,
                    committed_kw=float(amount),
                    eligible_hub_ids=eligible_hub_ids,
                    prior_granted_kw=prior_by_obligation.get(str(obligation_id)),
                    value_per_mwh=float(value_per_mwh) if value_per_mwh is not None else 0.0,
                )
            )
        return LedgerView(calls=tuple(calls))

    async def ledger_version(self) -> int:
        return await ledger.ledger_version()

    async def persist_grants(self, cycle_id: str, grants: Sequence[ProposedGrant]) -> None:
        if not grants:
            return
        version = await ledger.ledger_version()
        records = [
            GrantRecord(
                grant_id=uuid5(NAMESPACE_URL, f"{cycle_id}:{g.bank_id}:{g.obligation_id}:{g.is_headroom}"),
                cycle_id=cycle_id,
                bank_id=g.bank_id,
                granted_kw=Decimal(str(round(g.granted_kw, 3))),
                ledger_version=version,
                obligation_id=UUID(g.obligation_id) if g.obligation_id else None,
                is_headroom=g.is_headroom,
            )
            for g in grants
        ]
        await ledger.persist_grants(cycle_id, records)

    async def record_substitution(
        self, obligation_id: str, from_hub_id: str, to_hub_id: str, reason_code: str
    ) -> None:
        """Deferred (merge task item 2 targeted `run_cycle`'s automatic 2 s loop, which never calls
        this -- only `opengrid.allocator.substitute_hub`'s manual/API-triggered path does). `og.grant`
        has no `reason_code` column to record *why* a swap happened, so persisting one honestly needs a
        schema decision, not a guessed column -- raising rather than silently no-op-ing or inventing a
        shape (BUILD.md S5a "no silent fallbacks"). See qa/merge-notes.md."""
        raise NotImplementedError(
            "EngineLedgerGateway.record_substitution: og.grant has no reason_code column yet; "
            "see qa/merge-notes.md for the schema decision this needs before it can persist anything"
        )


def build_gateways(
    pool: AsyncConnectionPool,
) -> tuple[EngineFleetGateway, EngineLedgerGateway, EngineScadaGateway, EngineScheduleGateway]:
    """Convenience constructor for `opengrid.engine.main`: one of each gateway, built once per process
    (the fleet/scada gateways hold no state of their own; the ledger/schedule gateways hold the shared
    pool)."""
    return EngineFleetGateway(), EngineLedgerGateway(pool), EngineScadaGateway(), EngineScheduleGateway(pool)
