"""Q10 against the real schema: og-safestop's recorded utility lift (a SAFE_STOP trace row) is what the guardian's
release check reads back (`PgStopReleasePort.utility_lifts`), with the outstanding stop's reason."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.guardian.repo import PgStopReleasePort
from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync
from opengrid.safestop.trace_backend import PgSafestopTraceBackend
from opengrid.trace import TraceStore

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("OG_DB"), reason="needs the workspace database (tools/remote.ps1)"),
]


async def test_a_recorded_utility_lift_is_read_back_by_the_guardian():
    from opengrid.core.crypto import generate_keypair
    from opengrid.safestop.keys import StopSigningKey
    from opengrid.safestop.service import SafestopService

    dsn = build_dsn(load_config())
    migrate_sync(dsn)
    bank, lifted, lift_id = f"bank-q10-{uuid4().hex[:6]}", uuid4(), uuid4()
    async with AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False) as pool:
        seed, _ = generate_keypair()
        trace = TraceStore(PgSafestopTraceBackend(pool))
        service = SafestopService(StopSigningKey("safestop-it", seed), None, None, trace)  # type: ignore[arg-type]
        port = PgStopReleasePort(pool)
        assert await port.utility_lifts([bank]) == {}
        await service.record_utility_lift(bank, lifted, lift_id, "SCENARIO_ANOMALY")
        await service.record_utility_lift(bank, lifted, uuid4(), "SCENARIO_ANOMALY")  # a re-delivery later
        lifts = await port.utility_lifts([bank, "bank-other"])
        assert list(lifts) == [str(lifted)]
        assert await port.outstanding_engages("BANK", bank) == []  # the reason column is read too
