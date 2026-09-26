"""Operator-only admin actions that don't belong to any one domain screen (dispatch-live pass).

`POST /og/api/admin/seed-fleet-topology` re-runs `opengrid.fleet.seed.run_seed` on demand (it is also
run automatically by `deploy/scripts/deploy.sh` after every migration) -- an idempotent upsert of
`og.hub`/`og.bank` from `integration-sims/config/fleet.yaml`'s id scheme, useful after the simulator's
own config changes hub/bank counts without a full redeploy.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends
from psycopg_pool import AsyncConnectionPool

from opengrid.api.auth import Identity, require_operator
from opengrid.api.deps import get_config, get_pool
from opengrid.fleet.seed import run_seed
from opengrid.platform.config import Config

router = APIRouter(prefix="/og/api/admin", tags=["admin"])


@router.post("/seed-fleet-topology")
async def seed_fleet_topology(
    cfg: Annotated[Config, Depends(get_config)],
    pool: Annotated[AsyncConnectionPool, Depends(get_pool)],
    _identity: Annotated[Identity, Depends(require_operator)],
) -> dict[str, Any]:
    result = await run_seed(cfg, pool)
    return {"banks_upserted": result.banks_upserted, "hubs_upserted": result.hubs_upserted}
