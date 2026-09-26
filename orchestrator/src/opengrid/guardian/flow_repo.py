"""Postgres adapters for the guardian's discharge-flow (G-26..G-32) and territory (G-33) reads.

Static topology and premise rows (og.hub, og.service_transformer, og.feeder_limit, og.substation_limit,
og.bank, og.asset) are configuration, cached and refreshed every `refresh_s`. Flows are the guardian's own
live reads: the sum of the latest SCADA apparent-power reading of each member bank (import-positive, the
convention G-03 uses), with the OLDEST member reading's age -- one bank without a reading makes the
aggregate unknown (age inf), never a partial sum.
"""

from __future__ import annotations

import math
import time
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from uuid import UUID

from psycopg_pool import AsyncConnectionPool

from opengrid.core.models.market import UtilityId
from opengrid.guardian.config import GuardianConfig
from opengrid.guardian.ports import AggregateFlow, HubSite, ObligationMarket, PoiLimit, ServiceTransformer
from opengrid.market.config import load_zone_territory
from opengrid.settle.tariffs import resolve_tdsp_tariffs_path

DEFAULT_TOPOLOGY_REFRESH_S = 60.0

_HUB_SITES_SQL = """
SELECT hub_id, bank_id, export_limit_kw, service_kw, pv_rated_kw, peak_kw, tau_peak_s, transformer_id
FROM og.hub
"""
_TRANSFORMERS_SQL = "SELECT transformer_id, rating_kva FROM og.service_transformer"
_BANKS_SQL = "SELECT bank_id, zone, feeder_id FROM og.bank"
_FEEDER_LIMITS_SQL = "SELECT feeder_id, thermal_kw, reverse_kw FROM og.feeder_limit"
_SUBSTATION_LIMITS_SQL = "SELECT substation_id, rating_kva, reverse_kw FROM og.substation_limit"
_ASSETS_SQL = """
SELECT asset_id, asset_class, bank_id, substation_id, poi_import_kva, poi_export_kva
FROM og.asset WHERE status <> 'RETIRED'
"""
_AGGREGATE_FLOW_SQL = """
SELECT count(*), count(l.value), coalesce(sum(l.value), 0), max(l.age_s)
FROM unnest(%(banks)s::text[]) AS b(bank_id)
LEFT JOIN LATERAL (
    SELECT value, extract(epoch FROM now() - ts) AS age_s FROM og.feed_obs
    WHERE source = 'scada' AND product = b.bank_id AND series = 'APPARENT_POWER_KVA'
    ORDER BY ts DESC LIMIT 1
) l ON true
"""
_OBLIGATION_MARKET_SQL = """
SELECT c.market, c.utility_id, c.service_type
FROM og.obligation o JOIN og.contract c ON c.contract_id = o.contract_id
WHERE o.obligation_id = %(obligation_id)s
"""
_FREE_ACCESS_SQL = "SELECT free_access_granted FROM og.utility WHERE utility_id = %(utility_id)s"


@dataclass
class _Topology:
    sites: dict[str, HubSite] = field(default_factory=dict)
    bank_of_hub: dict[str, str] = field(default_factory=dict)
    zone_of_bank: dict[str, str] = field(default_factory=dict)
    transformers: dict[str, ServiceTransformer] = field(default_factory=dict)
    feeder_banks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    feeder_limits: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    substation_of_bank: dict[str, str] = field(default_factory=dict)
    substation_banks: dict[str, tuple[str, ...]] = field(default_factory=dict)
    substation_limits: dict[str, tuple[float | None, float | None]] = field(default_factory=dict)
    poi_by_bank: dict[str, PoiLimit] = field(default_factory=dict)


def _opt(value: object) -> float | None:
    return None if value is None else float(str(value))


class PgGridTopologyPort:
    """`GridTopologyPort` over Postgres. Premise columns that are NULL take the `[guardian.flow]` static
    defaults; a default configured as unknown stays None and the check fails closed."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        config: GuardianConfig,
        zone_territory: Mapping[str, UtilityId],
        *,
        refresh_s: float = DEFAULT_TOPOLOGY_REFRESH_S,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._config = config
        self._zone_territory = dict(zone_territory)
        self._refresh_s = refresh_s
        self._monotonic = monotonic_fn
        self._loaded_at: float | None = None
        self._topology = _Topology()

    async def _current(self) -> _Topology:
        now = self._monotonic()
        if self._loaded_at is None or now - self._loaded_at > self._refresh_s:
            self._topology = await self._load()
            self._loaded_at = now
        return self._topology

    async def _load(self) -> _Topology:
        cfg = self._config
        t = _Topology()
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_HUB_SITES_SQL)
            hub_rows = await cur.fetchall()
            await cur.execute(_TRANSFORMERS_SQL)
            transformer_rows = await cur.fetchall()
            await cur.execute(_BANKS_SQL)
            bank_rows = await cur.fetchall()
            await cur.execute(_FEEDER_LIMITS_SQL)
            feeder_rows = await cur.fetchall()
            await cur.execute(_SUBSTATION_LIMITS_SQL)
            substation_rows = await cur.fetchall()
            await cur.execute(_ASSETS_SQL)
            asset_rows = await cur.fetchall()
        members: dict[str, list[str]] = {}
        for hub_id, bank_id, export_kw, service_kw, pv_kw, peak_kw, tau_s, transformer_id in hub_rows:
            export = _opt(export_kw)
            t.sites[hub_id] = HubSite(
                export_limit_kw=export if export is not None else cfg.default_export_limit_kw,
                service_kw=_opt(service_kw) if service_kw is not None else cfg.default_service_kw,
                pv_rated_kw=float(pv_kw) if pv_kw is not None else cfg.default_pv_rated_kw,
                peak_kw=_opt(peak_kw),
                tau_peak_s=_opt(tau_s),
                transformer_id=transformer_id,
            )
            t.bank_of_hub[hub_id] = bank_id
            if transformer_id is not None:
                members.setdefault(transformer_id, []).append(hub_id)
        for transformer_id, rating in transformer_rows:
            t.transformers[transformer_id] = ServiceTransformer(
                transformer_id, float(rating), tuple(sorted(members.get(transformer_id, [])))
            )
        feeder_banks: dict[str, list[str]] = {}
        for bank_id, zone, feeder_id in bank_rows:
            t.zone_of_bank[bank_id] = zone
            if feeder_id is not None:
                feeder_banks.setdefault(feeder_id, []).append(bank_id)
        t.feeder_banks = {f: tuple(sorted(b)) for f, b in feeder_banks.items()}
        t.feeder_limits = {f: (_opt(th), _opt(rev)) for f, th, rev in feeder_rows}
        t.substation_limits = {s: (_opt(rating), _opt(rev)) for s, rating, rev in substation_rows}
        substation_banks: dict[str, list[str]] = {}
        for asset_id, asset_class, bank_id, substation_id, poi_import, poi_export in asset_rows:
            if bank_id is None:
                continue
            if asset_class == "SUBSTATION" and poi_import is not None and poi_export is not None:
                t.poi_by_bank[bank_id] = PoiLimit(asset_id, float(poi_import), float(poi_export))
            if substation_id is not None:
                t.substation_of_bank[bank_id] = substation_id
                substation_banks.setdefault(substation_id, []).append(bank_id)
        t.substation_banks = {s: tuple(sorted(set(b))) for s, b in substation_banks.items()}
        return t

    async def _aggregate(self, banks: tuple[str, ...]) -> tuple[float | None, float]:
        """(sum of the member banks' latest readings, oldest age); (None, inf) when any has none."""
        if not banks:
            return None, math.inf
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_AGGREGATE_FLOW_SQL, {"banks": list(banks)})
            row = await cur.fetchone()
        if row is None:
            return None, math.inf
        total, read, flow, age = row
        if read != total or age is None:
            return None, math.inf
        return float(flow), float(age)

    async def hub_site(self, hub_id: str) -> HubSite | None:
        return (await self._current()).sites.get(hub_id)

    async def transformer(self, transformer_id: str) -> ServiceTransformer | None:
        return (await self._current()).transformers.get(transformer_id)

    async def feeder_flow(self, feeder_id: str) -> AggregateFlow | None:
        t = await self._current()
        banks = t.feeder_banks.get(feeder_id, ())
        thermal, reverse = t.feeder_limits.get(
            feeder_id, (self._config.default_feeder_thermal_kw, self._config.default_feeder_reverse_kw)
        )
        flow, age = await self._aggregate(banks)
        return AggregateFlow(
            ref=feeder_id,
            flow_kw=flow,
            age_s=age,
            lower_kw=-reverse if reverse is not None else None,
            upper_kw=self._config.feeder_thermal_pct * thermal if thermal is not None else None,
            banks=banks,
        )

    async def substation_flow(self, bank_id: str) -> AggregateFlow | None:
        t = await self._current()
        substation_id = t.substation_of_bank.get(bank_id)
        if substation_id is None:
            return None
        banks = t.substation_banks.get(substation_id, ())
        rating, reverse = t.substation_limits.get(substation_id, (None, None))
        territory = self._zone_territory.get(t.zone_of_bank.get(bank_id, ""))
        if territory is not None:
            reverse = 0.0  # K15: no reverse flow at a regulated territory's substations
        flow, age = await self._aggregate(banks)
        return AggregateFlow(
            ref=substation_id,
            flow_kw=flow,
            age_s=age,
            lower_kw=-reverse if reverse is not None else None,
            upper_kw=self._config.substation_pct * rating if rating is not None else None,
            banks=banks,
        )

    async def territory_flow(self, bank_id: str) -> AggregateFlow | None:
        t = await self._current()
        utility = self._zone_territory.get(t.zone_of_bank.get(bank_id, ""))
        if utility is None:
            return None
        banks = tuple(sorted(b for b, z in t.zone_of_bank.items() if self._zone_territory.get(z) == utility))
        flow, age = await self._aggregate(banks)
        return AggregateFlow(
            ref=utility, flow_kw=flow, age_s=age, lower_kw=0.0, upper_kw=math.inf, banks=banks
        )

    async def poi_limit(self, bank_id: str) -> PoiLimit | None:
        return (await self._current()).poi_by_bank.get(bank_id)


class PgTerritoryPort:
    """`TerritoryPort`: the obligation's contract market (migration 0025), the hub's bank zone, and the
    utility's wholesale-access flag, each the guardian's own read."""

    def __init__(self, pool: AsyncConnectionPool, zone_territory: Mapping[str, UtilityId]) -> None:
        self._pool = pool
        self._zone_territory = dict(zone_territory)
        self._zone_of_hub: dict[str, str] | None = None
        self._free_access: dict[str, tuple[bool, float]] = {}

    def zone_territory(self) -> Mapping[str, UtilityId]:
        return self._zone_territory

    async def hub_zone(self, hub_id: str) -> str | None:
        if self._zone_of_hub is None or hub_id not in self._zone_of_hub:
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(
                    "SELECT h.hub_id, b.zone FROM og.hub h JOIN og.bank b ON b.bank_id = h.bank_id"
                )
                self._zone_of_hub = {str(h): str(z) for h, z in await cur.fetchall()}
        return self._zone_of_hub.get(hub_id)

    async def obligation_market(self, obligation_id: UUID) -> ObligationMarket | None:
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_OBLIGATION_MARKET_SQL, {"obligation_id": obligation_id})
            row = await cur.fetchone()
        if row is None:
            return None
        return ObligationMarket(market=row[0], utility_id=row[1], service_type=row[2])

    async def free_access(self, utility_id: str) -> bool:
        cached = self._free_access.get(utility_id)
        if cached is not None and time.monotonic() - cached[1] < DEFAULT_TOPOLOGY_REFRESH_S:
            return cached[0]
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_FREE_ACCESS_SQL, {"utility_id": utility_id})
            row = await cur.fetchone()
        granted = bool(row and row[0])
        self._free_access[utility_id] = (granted, time.monotonic())
        return granted


def load_required_zone_territory() -> dict[str, UtilityId]:
    """`[zone_territory]` of tdsp_tariffs.toml (`market.config.load_zone_territory`, path resolved as settle
    resolves it). A missing file raises at startup: without the table a regulated zone would read as the
    competitive area (K15 must not fail open)."""
    return load_zone_territory(resolve_tdsp_tariffs_path())
