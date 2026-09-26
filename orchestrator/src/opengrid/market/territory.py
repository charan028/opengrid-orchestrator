"""Market membership and territory resolution (08 S3.1, 09 D1/D2, invariant K15).

- `market_of` / `market_of_contract`: which market an obligation's contract belongs to.
- `territory_of_zone`: which territory an asset in a given ERCOT settlement zone sits in.
- `check_territory`: the K15 predicate -- may an asset in territory T serve an obligation in market M?
  It is the ONE implementation; the selector (C25), the allocator eligibility filter, the guardian
  (G-33, on its own reads) and the invariants checker (`K15_TERRITORY_MARKET`) all call it.

Pure: no I/O. Missing data never passes: an unknown territory or an inconsistent market is a failure,
never a guess (K15 fail-safe: VETO).
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Final

from opengrid.core.models.engine import Contract
from opengrid.core.models.market import (
    ERCOT_COMPETITIVE,
    UTILITY_IDS,
    Market,
    Territory,
    UtilityId,
)

#: The asset sits outside the regulated utility's territory (K15 a), or a regulated-territory asset
#: would serve a different utility.
R_TERRITORY_OUTSIDE: Final = "R-TERRITORY-OUTSIDE"
#: A regulated-territory asset would take a FREE (ERCOT) opportunity without wholesale access (K15 b).
R_TERRITORY_NO_FREE_ACCESS: Final = "R-TERRITORY-NO-FREE-ACCESS"
#: The asset's territory or the obligation's market is unknown: fail closed.
R_TERRITORY_UNKNOWN: Final = "R-TERRITORY-UNKNOWN"

TERRITORY_REASONS: Final = frozenset({R_TERRITORY_OUTSIDE, R_TERRITORY_NO_FREE_ACCESS, R_TERRITORY_UNKNOWN})

#: Service types that only exist in the regulated market.
REGULATED_ONLY_SERVICE_TYPES: Final = frozenset({"REGULATED_CAPACITY"})


class MarketModelError(ValueError):
    """Inconsistent or missing market data. Callers treat it as ineligible (fail closed)."""


@dataclass(frozen=True, slots=True)
class MarketRef:
    """The market of a contract (and so of each of its obligations): `REGULATED` with its utility, or
    `FREE` (ERCOT) with `utility_id=None`."""

    market: Market
    utility_id: UtilityId | None = None

    def __post_init__(self) -> None:
        if self.market == "REGULATED" and self.utility_id is None:
            raise MarketModelError("a REGULATED market reference needs a utility_id")
        if self.market == "FREE" and self.utility_id is not None:
            raise MarketModelError(f"a FREE market reference cannot name a utility (got {self.utility_id})")

    @property
    def is_regulated(self) -> bool:
        return self.market == "REGULATED"


FREE: Final = MarketRef("FREE")


def _as_utility_id(value: str) -> UtilityId:
    for utility_id in UTILITY_IDS:
        if value == utility_id:
            return utility_id
    raise MarketModelError(f"unknown utility_id {value!r}")


def market_of(*, market: str | None, utility_id: str | None, service_type: str | None = None) -> MarketRef:
    """The market of an obligation, from its contract's `market`/`utility_id` columns (migration 0025).

    - `market` NULL (a row read before 0025, or a caller without the column) means FREE, the column's
      default -- unless `service_type` is REGULATED_CAPACITY, which only exists in a regulated market.
    - A REGULATED_CAPACITY contract that is not REGULATED, a REGULATED contract without a utility, or a
      FREE contract naming a utility raise `MarketModelError`: never guess (fail closed)."""
    if market is None:
        if service_type in REGULATED_ONLY_SERVICE_TYPES:
            raise MarketModelError(f"{service_type} contract without a market (expected REGULATED)")
        if utility_id is not None:
            raise MarketModelError("utility_id set but market missing")
        return FREE
    if market == "FREE":
        if service_type in REGULATED_ONLY_SERVICE_TYPES:
            raise MarketModelError(f"{service_type} contract cannot be in the FREE market")
        if utility_id is not None:
            raise MarketModelError(f"a FREE contract cannot name a utility (got {utility_id})")
        return FREE
    if market == "REGULATED":
        if utility_id is None:
            raise MarketModelError("REGULATED contract without a utility_id")
        return MarketRef("REGULATED", _as_utility_id(utility_id))
    raise MarketModelError(f"unknown market {market!r}")


def market_of_contract(contract: Contract) -> MarketRef:
    """`market_of` for a `Contract` row."""
    return market_of(
        market=contract.market, utility_id=contract.utility_id, service_type=contract.service_type
    )


def territory_of_zone(zone: str | None, zone_territory: Mapping[str, UtilityId]) -> Territory | None:
    """The territory of an asset in settlement zone `zone`: the regulated utility that owns the zone in
    `tdsp_tariffs.toml`'s `[zone_territory]` table, else the ERCOT competitive area when the zone is a
    known competitive load zone (`LZ_*`). `None` when `zone` is missing or not recognisable -- callers
    treat `None` as not eligible for anything (K15 fail-safe)."""
    if not zone:
        return None
    utility = zone_territory.get(zone)
    if utility is not None:
        return utility
    if zone.startswith("LZ_"):
        return ERCOT_COMPETITIVE
    return None


def utility_of_territory(territory: str | None) -> UtilityId | None:
    """The regulated utility a territory value names, or `None` (competitive area, unknown, missing)."""
    return next((u for u in UTILITY_IDS if u == territory), None)


_KNOWN_TERRITORIES: Final = frozenset({*UTILITY_IDS, ERCOT_COMPETITIVE})


def check_territory(
    ref: MarketRef | None,
    asset_territory: Territory | str | None,
    *,
    free_access: bool = False,
) -> str | None:
    """K15 (09 D2 a/b): may an asset in `asset_territory` serve an obligation in market `ref`?

    Returns `None` when allowed, else the reason code:
    - `R-TERRITORY-UNKNOWN`: the market or the asset's territory is unknown (fail closed);
    - `R-TERRITORY-OUTSIDE`: REG(u) served by an asset outside u's territory;
    - `R-TERRITORY-NO-FREE-ACCESS`: FREE (or headroom) served by an asset inside a regulated territory
      whose utility has not granted wholesale access (`free_access`, default false, 09 OQ-5).

    Headroom (no obligation) is FREE: pass `FREE`. `free_access` is the access flag of the utility
    that owns `asset_territory` (ignored for competitive-area assets). `asset_territory` may be a raw
    DB string; anything other than a known territory value is unknown."""
    if ref is None or asset_territory not in _KNOWN_TERRITORIES:
        return R_TERRITORY_UNKNOWN
    if ref.market == "REGULATED":
        return None if asset_territory == ref.utility_id else R_TERRITORY_OUTSIDE
    if asset_territory == ERCOT_COMPETITIVE or free_access:
        return None
    return R_TERRITORY_NO_FREE_ACCESS
