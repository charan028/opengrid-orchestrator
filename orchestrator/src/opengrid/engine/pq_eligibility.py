"""PQ eligibility of hubs per PQ-sensitive service profile (WP-D wiring, 06-service-profiles S5.2).

Owner decision 2026-09-26: the S5.2 filter (`opengrid.allocator.pq_eligibility`) applies to the hubs a
PQ-sensitive profile (`[eligibility].pq_aware_selection = true`, e.g. DATA_CENTER) may draw from; arbitrage
and ERCOT products are unaffected. This module keeps the per-profile eligible hub set and eligible kW per
bank, refreshed from `og.hub_inverter_pq` (characterized every 5 min) plus live hub health:

- the allocator narrows such an obligation's `eligible_hub_ids` to it;
- the selector never commits such an obligation beyond the eligible kW (no silent overcommit);
- `og_engine_pq_eligible_kw{service_type}` exposes the eligible capacity.

A profile with no characterization data yet has NO eligible hubs (fail closed for PQ-sensitive work).
"""

from __future__ import annotations

import logging
import tomllib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from prometheus_client import Gauge
from psycopg_pool import AsyncConnectionPool

from opengrid.allocator.pq_eligibility import EligibilityConfig, HubPqCandidate, evaluate_hub_eligibility

logger = logging.getLogger(__name__)

PROFILES_DIR = Path(__file__).resolve().parents[3] / "config" / "service_profiles"

pq_eligible_kw = Gauge(
    "og_engine_pq_eligible_kw",
    "Rated discharge kW of online hubs eligible for a PQ-sensitive service profile (S5.2).",
    labelnames=("service_type",),
)
pq_eligible_hubs = Gauge(
    "og_engine_pq_eligible_hubs",
    "Online hubs eligible for a PQ-sensitive service profile (S5.2).",
    labelnames=("service_type",),
)

_ROWS_SQL = """
SELECT p.hub_id, h.bank_id, h.p_kw, p.phase_connection, p.kva_rating, p.pf_min_leading, p.pf_min_lagging,
       p.freq_offset_hz, p.voltage_offset_pct, p.thd_current_pct, p.phase_angle_error_deg, p.quality_score,
       p.ride_through_class, p.asset_state, coalesce(s.health, 'offline')
FROM og.hub_inverter_pq p
JOIN og.hub h USING (hub_id)
LEFT JOIN og.hub_state s USING (hub_id)
"""


def load_profile_configs(directory: Path = PROFILES_DIR) -> dict[str, EligibilityConfig]:
    """`service_type -> EligibilityConfig` for every profile whose `[eligibility].pq_aware_selection` is on."""
    configs: dict[str, EligibilityConfig] = {}
    for path in sorted(directory.glob("*.toml")):
        with path.open("rb") as fh:
            data = tomllib.load(fh)
        elig = data.get("eligibility")
        service_type = data.get("profile", {}).get("service_type")
        if not elig or not service_type or not elig.get("pq_aware_selection"):
            continue
        configs[str(service_type)] = EligibilityConfig(
            pq_aware_selection=True,
            min_quality_score=float(elig["min_quality_score"]),
            required_ride_through_class=str(elig["required_ride_through_class"]),
            eligible_asset_states=frozenset(elig["eligible_asset_states"]),
            excluded_asset_states=frozenset(elig["excluded_asset_states"]),
        )
    return configs


@dataclass
class _ProfileEligibility:
    hubs_by_bank: dict[str, set[str]] = field(default_factory=dict)
    kw_by_bank: dict[str, float] = field(default_factory=dict)


_configs: dict[str, EligibilityConfig] = {}
_state: dict[str, _ProfileEligibility] = {}


def configure(configs: dict[str, EligibilityConfig]) -> None:
    global _configs
    _configs = dict(configs)
    _state.clear()


def is_pq_sensitive(service_type: str) -> bool:
    return service_type in _configs


def compute(
    rows: list[tuple[Any, ...]], configs: dict[str, EligibilityConfig]
) -> dict[str, _ProfileEligibility]:
    """Pure: evaluate every profile over the online hubs in `rows` (`_ROWS_SQL` shape)."""
    online = [r for r in rows if r[14] == "online"]
    bank_of = {r[0]: r[1] for r in online}
    rated_of = {r[0]: float(r[2]) for r in online}
    candidates = [
        HubPqCandidate(
            hub_id=r[0],
            phase_connection=r[3],
            kva_rating=float(r[4]),
            pf_min_leading=float(r[5]),
            pf_min_lagging=float(r[6]),
            freq_offset_hz=float(r[7]),
            voltage_offset_pct=float(r[8]),
            thd_current_pct=float(r[9]),
            phase_angle_error_deg=float(r[10]),
            quality_score=float(r[11]),
            ride_through_class=r[12],
            asset_state=r[13],
        )
        for r in online
    ]
    result: dict[str, _ProfileEligibility] = {}
    for service_type, cfg in configs.items():
        state = _ProfileEligibility()
        for hub_id in evaluate_hub_eligibility(candidates, cfg).eligible_hub_ids:
            bank_id = bank_of[hub_id]
            state.hubs_by_bank.setdefault(bank_id, set()).add(hub_id)
            state.kw_by_bank[bank_id] = state.kw_by_bank.get(bank_id, 0.0) + rated_of[hub_id]
        result[service_type] = state
    return result


async def refresh(pool: AsyncConnectionPool) -> None:
    """Re-read characterization + health and recompute every profile (one query)."""
    if not _configs:
        return
    async with pool.connection() as conn, conn.cursor() as cur:
        await cur.execute(_ROWS_SQL)
        rows = await cur.fetchall()
    computed = compute(rows, _configs)
    _state.clear()
    _state.update(computed)
    for service_type, state in computed.items():
        pq_eligible_kw.labels(service_type=service_type).set(sum(state.kw_by_bank.values()))
        pq_eligible_hubs.labels(service_type=service_type).set(
            sum(len(h) for h in state.hubs_by_bank.values())
        )


def filter_hub_ids(service_type: str, bank_id: str, hub_ids: tuple[str, ...]) -> tuple[str, ...]:
    """`hub_ids` narrowed to the profile's eligible hubs on `bank_id`; unchanged for other services."""
    if service_type not in _configs:
        return hub_ids
    allowed = _state.get(service_type, _ProfileEligibility()).hubs_by_bank.get(bank_id, set())
    return tuple(h for h in hub_ids if h in allowed)


def eligible_kw(service_type: str, bank_id: str) -> float | None:
    """Eligible rated kW on `bank_id` for a PQ-sensitive profile; `None` for other services."""
    if service_type not in _configs:
        return None
    return _state.get(service_type, _ProfileEligibility()).kw_by_bank.get(bank_id, 0.0)
