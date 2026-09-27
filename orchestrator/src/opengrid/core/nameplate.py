"""The ONE rule for which hubs are rated at their nameplate `og.hub.p_kw` instead of the home per-unit
cap (11 kW per unit, `core.limits.continuous_power_kw`): a hub that belongs to an `og.asset` row of a
nameplate class -- the D-29 substation battery set (SUBSTATION) or a D-31 truck (MOBILE_STORAGE, migration
0044) -- linked by its (one-hub) bank or by its own id. Home hubs have no such row and keep the home rules.

Every reader of that fact uses these fragments -- the guardian (G-02), the fleet twin the selector and the
allocator plan with, the engine's utility-scale bank set and the invariants -- so the planners and the
guardian can never disagree on a truck's rating.
"""

from __future__ import annotations

#: Asset classes rated at nameplate.
NAMEPLATE_ASSET_CLASSES: tuple[str, ...] = ("SUBSTATION", "MOBILE_STORAGE")

#: A boolean SQL expression over `og.hub h`: the hub is nameplate-rated. Literal SQL (the class list is spelled
#: out; `tests/unit/core/test_nameplate.py` keeps it equal to `NAMEPLATE_ASSET_CLASSES`).
NAMEPLATE_HUB_EXISTS_SQL = (
    "EXISTS (SELECT 1 FROM og.asset a WHERE (a.bank_id = h.bank_id OR a.asset_id = h.hub_id)"
    " AND a.asset_class IN ('SUBSTATION', 'MOBILE_STORAGE'))"
)

#: Banks carrying a nameplate-rated asset (every hub on them is rated at nameplate).
NAMEPLATE_BANKS_SQL = (
    "SELECT DISTINCT bank_id FROM og.asset"
    " WHERE asset_class IN ('SUBSTATION', 'MOBILE_STORAGE') AND bank_id IS NOT NULL"
)
