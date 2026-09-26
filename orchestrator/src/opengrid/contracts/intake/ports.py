"""I/O port `opengrid.contracts.intake` needs for live market data (02a S1-S3, BUILD.md S5a "pure
logic separated from I/O"). Mirrors `opengrid.engine.gateways`' own reasoning: intake runs inside the
`og-engine` process, where `opengrid.feeds.latest()`/`window()` are unusable (that module's `FeedStore`
singleton is only ever configured in the separate `og-feeds` process) -- the same table (`og.feed_obs`)
is read directly here instead, which is the correct cross-process boundary
(`opengrid.engine.gateways`'s own docstring makes the identical argument for the allocator's price
read).

`PgMarketDataPort` is the real implementation; unit tests use `FakeMarketDataPort`
(`tests/unit/contracts/intake/fakes.py`) so the pure candidate-generation math in `energy.py`/
`ancillary.py` is testable with hand-computed prices, no database required.
"""

from __future__ import annotations

from typing import Protocol

#: `opengrid.feeds.ercot`'s product codes (PRODUCT_PATHS) -- the same strings `og.feed_obs.product`
#: stores, kept here as named constants rather than re-typed at each call site (BUILD.md S5a "named
#: constants with units in the name").
ENERGY_PRICE_PRODUCT = "np6-905-cd"
AS_PRICE_PRODUCT = "np4-188-cd"
FEED_SOURCE_ERCOT = "ERCOT"


class MarketDataPort(Protocol):
    """Live-price reads `opengrid.contracts.intake` needs. Both methods return `None` (never raise)
    when no observation exists yet -- a cold `feed_obs` table degrades intake to "nothing to offer
    this gate" rather than crashing the gate scheduler (K7 "degrade, don't trip")."""

    async def latest_energy_price_usd_per_mwh(self, series_key: str) -> float | None:
        """Most recent `np6-905-cd` (real-time settlement-point price) observation for `series_key`
        (an ERCOT load-zone/hub code, e.g. `"LZ_HOUSTON"`)."""
        ...

    async def latest_as_mcpc_usd_per_mwh(self, product_code: str) -> float | None:
        """Most recent `np4-188-cd` (day-ahead AS clearing price) observation whose `series` equals
        `product_code` (ERCOT's `ancillaryType`, e.g. `"REGUP"`/`"NONSPIN"` -- the same string a
        contract's `og.product_rule.product_code` carries for an ERCOT_AS contract)."""
        ...
