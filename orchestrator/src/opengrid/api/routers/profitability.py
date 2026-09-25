"""Profitability screen (02b S7.1, S8 screen 6): reads `pnl` rows `settle` posts -- this module never
computes a baseline or margin itself (02b S12: "the UI's Profitability screen only reads `pnl` rows").
"""

from __future__ import annotations

from datetime import date
from typing import Annotated, Any

from fastapi import APIRouter, Depends

from opengrid.api.auth import Identity, require_viewer
from opengrid.api.deps import get_store
from opengrid.api.store import StoreProtocol

router = APIRouter(prefix="/og/api/profitability", tags=["profitability"])


@router.get("/summary")
async def profitability_summary(
    store: Annotated[StoreProtocol, Depends(get_store)],
    _identity: Annotated[Identity, Depends(require_viewer)],
    service: str | None = None,
    day: date | None = None,
) -> list[dict[str, Any]]:
    """Per-obligation-interval revenue, energy cost, degradation, penalty, net margin,
    LP-vs-rule-baseline, and forgone-upside-from-lock (02b S7.1, S8 screen 6). Row shape matches
    `opengrid.ui.routes.profitability.profitability_table_view`, which keys rows by `obligation_id`
    (the screen and its own totals/chart aggregate for display, this endpoint does not)."""
    rows = await store.profitability_summary(service=service, day=day)
    return [
        {
            "obligation_id": row["obligation_id"],
            "service_type": row["service_type"],
            "contract_id": row["contract_id"],
            "interval_start": row["interval_start"].isoformat(),
            "interval_end": row["interval_end"].isoformat(),
            "revenue": row["revenue"],
            "energy_cost": row["energy_cost"],
            "degradation_cost": row["degradation_cost"],
            "penalty": row["penalty"],
            "net_value": row["net_value"],
            "rule_baseline_value": row["rule_baseline_value"],
            "forgone_upside": row["forgone_upside"],
        }
        for row in rows
    ]
