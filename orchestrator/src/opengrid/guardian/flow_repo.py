"""Postgres adapters for the guardian's discharge-flow (G-26..G-32) and territory (G-33) reads.

Static topology and premise rows (og.hub, og.service_transformer, og.feeder_limit, og.substation_limit,
og.bank, og.asset) are configuration, cached and refreshed every `refresh_s`. Flows are the guardian's own
live reads, import-positive, summed over the member banks with the OLDEST member reading's age -- one bank
without a usable reading makes the aggregate unknown (age inf), never a partial sum.

Only GOOD-quality SCADA rows count, a row stamped more than `FUTURE_TOLERANCE_S` ahead of the database clock
is ignored, and a reading older than `[guardian.flow].max_age_s` is missing. Per bank:

- `REAL_POWER_KW` (signed, + = the bank draws from the feeder) is exact;
- otherwise `APPARENT_POWER_KVA` is only a magnitude m (kVA is unsigned), so the bank flow F is an interval.
  F <= m always. The export side is bounded by the bank's net hub power from the guardian's OWN telemetry
  (`BankMembersPort`): home load is >= 0, so F >= sum(hub p) - PV rated (a hub not online counts at full
  discharge; a hub with no og.hub.pv_rated_kw takes the conservative `default_pv_rated_kw`, never 0 -- H3).
  With no hub read the export side is -m: an unknown direction is treated as export (09 S2.6 fail closed).
  Production stores REAL_POWER_KW for every bank, so the kVA path is the fallback when it is bad or stale.
  `scada_min_power_factor` (|F| >= pf * m) excludes (-pf m, pf m) once the floor rules out
  export.
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
from opengrid.guardian.ports import (
    AggregateFlow,
    BankMembersPort,
    HubSite,
    ObligationMarket,
    PoiLimit,
    ServiceTransformer,
)
from opengrid.market.config import load_zone_territory
from opengrid.settle.tariffs import resolve_tdsp_tariffs_path

DEFAULT_TOPOLOGY_REFRESH_S = 60.0
#: SCADA rows stamped further than this ahead of the database clock are ignored (clock skew / bad stamps).
FUTURE_TOLERANCE_S = 5.0
#: An unknown hub triggers a hub->zone reload at most this often (between the regular refreshes).
_MISS_RELOAD_MIN_S = 5.0

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
#: Per bank: the latest GOOD, not-future-stamped signed real power and apparent power, with their ages.
_AGGREGATE_FLOW_SQL = """
SELECT b.bank_id, r.value, r.age_s, a.value, a.age_s
FROM unnest(%(banks)s::text[]) AS b(bank_id)
LEFT JOIN LATERAL (
    SELECT value, extract(epoch FROM now() - ts) AS age_s FROM og.feed_obs
    WHERE source = 'scada' AND product = b.bank_id AND series = 'REAL_POWER_KW' AND quality = 'GOOD'
      AND ts <= now() + make_interval(secs => %(future_s)s)
    ORDER BY ts DESC LIMIT 1
) r ON true
LEFT JOIN LATERAL (
    SELECT value, extract(epoch FROM now() - ts) AS age_s FROM og.feed_obs
    WHERE source = 'scada' AND product = b.bank_id AND series = 'APPARENT_POWER_KVA' AND quality = 'GOOD'
      AND ts <= now() + make_interval(secs => %(future_s)s)
    ORDER BY ts DESC LIMIT 1
) a ON true
"""
_HUB_ZONES_SQL = "SELECT h.hub_id, b.zone FROM og.hub h JOIN og.bank b ON b.bank_id = h.bank_id"
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


def _usable(value: object, age_s: object, max_age_s: float) -> tuple[float, float] | None:
    """(value, age >= 0) of a reading that exists and is fresh; None when missing or stale (treated alike)."""
    if value is None or age_s is None:
        return None
    reading, age = float(str(value)), max(float(str(age_s)), 0.0)
    if not math.isfinite(reading) or age > max_age_s:
        return None
    return reading, age


def bank_flow_interval_kw(
    magnitude_kva: float, net_floor_kw: float | None, min_power_factor: float
) -> tuple[float, float]:
    """(export-side, import-side) bounds of a bank's real flow F from an unsigned apparent-power reading m.
    F <= m; F >= max(floor, -m) (floor = sum(hub p) - PV: home load >= 0), -m when the floor is unknown; and
    when the floor alone rules out |F| < pf * m on the export side, F >= pf * m."""
    m = abs(magnitude_kva)
    low = -m if net_floor_kw is None else max(net_floor_kw, -m)
    if low > -min_power_factor * m:
        low = max(low, min_power_factor * m)
    return min(low, m), m


class PgGridTopologyPort:
    """`GridTopologyPort` over Postgres. Premise columns that are NULL take the `[guardian.flow]` static
    defaults; a default configured as unknown stays None and the check fails closed. `members` is the
    guardian's own hub telemetry (the MQTT cache), used to bound an unsigned kVA reading's export side;
    None: every kVA-only bank's direction is unknown (treated as export)."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        config: GuardianConfig,
        zone_territory: Mapping[str, UtilityId],
        *,
        members: BankMembersPort | None = None,
        refresh_s: float = DEFAULT_TOPOLOGY_REFRESH_S,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._config = config
        self._zone_territory = dict(zone_territory)
        self._members = members
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

    async def _net_floor_kw(self, bank_id: str, t: _Topology) -> float | None:
        """The export-side floor of the bank's flow from the guardian's own hub telemetry: sum(hub p) - PV
        rated, a hub not online at full discharge. None when the membership or any member cannot be read."""
        if self._members is None:
            return None
        hub_ids = await self._members.member_hub_ids(bank_id)
        snapshots = await self._members.member_snapshots(bank_id)
        if not hub_ids or len(snapshots) != len(hub_ids):
            return None
        net = sum(s.prev_p_kw if s.health == "online" else -max(s.params.p_kw, 0.0) for s in snapshots)
        pv = sum(
            site.pv_rated_kw if (site := t.sites.get(h)) is not None else self._config.default_pv_rated_kw
            for h in hub_ids
        )
        return net - pv

    async def _aggregate(
        self, banks: tuple[str, ...], t: _Topology
    ) -> tuple[float | None, float | None, float]:
        """(import-side sum, export-side sum, oldest age) over the member banks; (None, None, inf) when any
        bank has no usable reading."""
        unknown: tuple[None, None, float] = (None, None, math.inf)
        if not banks:
            return unknown
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_AGGREGATE_FLOW_SQL, {"banks": list(banks), "future_s": FUTURE_TOLERANCE_S})
            rows = {str(row[0]): row for row in await cur.fetchall()}
        max_age_s = self._config.flow_max_age_s
        high = low = oldest = 0.0
        for bank_id in banks:
            row = rows.get(bank_id)
            if row is None:
                return unknown
            real = _usable(row[1], row[2], max_age_s)
            if real is not None:
                high, low, oldest = high + real[0], low + real[0], max(oldest, real[1])
                continue
            kva = _usable(row[3], row[4], max_age_s)
            if kva is None:
                return unknown
            bank_low, bank_high = bank_flow_interval_kw(
                kva[0], await self._net_floor_kw(bank_id, t), self._config.scada_min_power_factor
            )
            high, low, oldest = high + bank_high, low + bank_low, max(oldest, kva[1])
        return high, low, oldest

    async def hub_site(self, hub_id: str) -> HubSite | None:
        return (await self._current()).sites.get(hub_id)

    async def hub_bank(self, hub_id: str) -> str | None:
        """The bank og.hub places the hub on (refreshed with the topology); None for an unknown hub."""
        return (await self._current()).bank_of_hub.get(hub_id)

    async def transformer(self, transformer_id: str) -> ServiceTransformer | None:
        return (await self._current()).transformers.get(transformer_id)

    async def feeder_flow(self, feeder_id: str) -> AggregateFlow | None:
        t = await self._current()
        banks = t.feeder_banks.get(feeder_id, ())
        cfg = self._config
        # 09 S2.6: with `fail_closed_missing_topology` a feeder with no og.feeder_limit row has unknown
        # limits (any increase vetoed); off (R3 default, the sim fleet has no rows yet) the static defaults.
        defaults = (
            (None, None)
            if cfg.flow_fail_closed_missing_topology
            else (cfg.default_feeder_thermal_kw, cfg.default_feeder_reverse_kw)
        )
        thermal, reverse = t.feeder_limits.get(feeder_id, defaults)
        flow, flow_low, age = await self._aggregate(banks, t)
        return AggregateFlow(
            ref=feeder_id,
            flow_kw=flow,
            flow_low_kw=flow_low,
            age_s=age,
            lower_kw=-reverse if reverse is not None else None,
            upper_kw=self._config.feeder_thermal_pct * thermal if thermal is not None else None,
            banks=banks,
        )

    async def substation_flow(self, bank_id: str) -> AggregateFlow | None:
        """None when the bank's substation topology is missing: no og.asset mapping (any asset class, so the
        HOME_BANK rows map home banks too), or -- unless `fail_closed_missing_topology`, which makes the
        limits unknown (increases vetoed) -- no og.substation_limit row for the mapped substation. The service
        raises ALR-SUBSTATION-UNMAPPED-TOPOLOGY whenever G-29 is skipped."""
        t = await self._current()
        substation_id = t.substation_of_bank.get(bank_id)
        if substation_id is None:
            return None
        banks = t.substation_banks.get(substation_id, ())
        limits = t.substation_limits.get(substation_id)
        if limits is None and not self._config.flow_fail_closed_missing_topology:
            return None
        rating, reverse = limits if limits is not None else (None, None)
        territory = self._zone_territory.get(t.zone_of_bank.get(bank_id, ""))
        if territory is not None:
            reverse = 0.0  # K15: no reverse flow at a regulated territory's substations
        flow, flow_low, age = await self._aggregate(banks, t)
        return AggregateFlow(
            ref=substation_id,
            flow_kw=flow,
            flow_low_kw=flow_low,
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
        flow, flow_low, age = await self._aggregate(banks, t)
        return AggregateFlow(
            ref=utility,
            flow_kw=flow,
            flow_low_kw=flow_low,
            age_s=age,
            lower_kw=0.0,
            upper_kw=math.inf,
            banks=banks,
        )

    async def poi_limit(self, bank_id: str) -> PoiLimit | None:
        return (await self._current()).poi_by_bank.get(bank_id)


class PgTerritoryPort:
    """`TerritoryPort`: the obligation's contract market (migration 0025), the hub's bank zone, and the
    utility's wholesale-access flag, each the guardian's own read."""

    def __init__(
        self,
        pool: AsyncConnectionPool,
        zone_territory: Mapping[str, UtilityId],
        *,
        refresh_s: float = DEFAULT_TOPOLOGY_REFRESH_S,
        monotonic_fn: Callable[[], float] = time.monotonic,
    ) -> None:
        self._pool = pool
        self._zone_territory = dict(zone_territory)
        self._refresh_s = refresh_s
        self._monotonic = monotonic_fn
        self._zone_of_hub: dict[str, str] = {}
        self._zones_loaded_at: float | None = None
        self._free_access: dict[str, tuple[bool, float]] = {}

    def zone_territory(self) -> Mapping[str, UtilityId]:
        return self._zone_territory

    async def hub_zone(self, hub_id: str) -> str | None:
        """Refreshed on the topology's cadence (a hub moved to another bank is seen within `refresh_s`);
        an unknown hub also triggers a reload, at most every `_MISS_RELOAD_MIN_S`."""
        now = self._monotonic()
        age = math.inf if self._zones_loaded_at is None else now - self._zones_loaded_at
        if age > self._refresh_s or (hub_id not in self._zone_of_hub and age > _MISS_RELOAD_MIN_S):
            async with self._pool.connection() as conn, conn.cursor() as cur:
                await cur.execute(_HUB_ZONES_SQL)
                self._zone_of_hub = {str(h): str(z) for h, z in await cur.fetchall()}
            self._zones_loaded_at = now
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
        if cached is not None and self._monotonic() - cached[1] < self._refresh_s:
            return cached[0]
        async with self._pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(_FREE_ACCESS_SQL, {"utility_id": utility_id})
            row = await cur.fetchone()
        granted = bool(row and row[0])
        self._free_access[utility_id] = (granted, self._monotonic())
        return granted


def load_required_zone_territory() -> dict[str, UtilityId]:
    """`[zone_territory]` of tdsp_tariffs.toml (`market.config.load_zone_territory`, path resolved as settle
    resolves it). A missing file raises at startup: without the table a regulated zone would read as the
    competitive area (K15 must not fail open)."""
    return load_zone_territory(resolve_tdsp_tariffs_path())
