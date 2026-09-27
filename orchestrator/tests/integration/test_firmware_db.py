"""Migration 0037 and the firmware SQL paths against real Postgres (R3.1): the API/executor repo
(`PgFirmwareRepo`), the guardian hand-off port (`PgFirmwareGuardianPort`: G-36 facts, (epoch, seq)
reservation, signed/published marks) and the hub status ingest. Server only (`tools/remote.ps1 -Ws fw`);
every row it writes carries an `fwit-` id and is removed afterwards."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from datetime import UTC, datetime, timedelta

import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.skipif(
    not os.environ.get("OG_DB"), reason="requires the server environment (tools/remote.ps1)"
)

SHA = "c" * 64
HUBS = ("fwit-hub-1", "fwit-hub-2")


@pytest.fixture(scope="module")
def dsn(server_config, _migrated) -> str:  # type: ignore[no-untyped-def]
    from opengrid.platform.db import build_dsn

    return str(build_dsn(server_config))


@pytest.fixture
async def pool(dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


def _cleanup(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        ids = "SELECT campaign_id FROM og.firmware_campaign WHERE name LIKE 'fwit-%%'"
        conn.execute(f"DELETE FROM og.firmware_job_event WHERE campaign_id IN ({ids})")  # noqa: S608
        conn.execute(f"DELETE FROM og.firmware_command WHERE campaign_id IN ({ids})")  # noqa: S608
        conn.execute(f"DELETE FROM og.firmware_job WHERE campaign_id IN ({ids})")  # noqa: S608
        conn.execute("DELETE FROM og.firmware_campaign WHERE name LIKE 'fwit-%%'")
        conn.execute("DELETE FROM og.firmware_catalogue WHERE release_note LIKE 'fwit-%%'")
        conn.execute("DELETE FROM og.hub_state WHERE hub_id LIKE 'fwit-%%'")
        conn.execute("DELETE FROM og.hub WHERE hub_id LIKE 'fwit-%%'")
        conn.execute("DELETE FROM og.bank WHERE bank_id LIKE 'fwit-%%'")


async def test_firmware_campaign_round_trip(dsn: str, pool: AsyncConnectionPool) -> None:
    from opengrid.firmware import campaigns as svc
    from opengrid.firmware.catalogue import Catalogue
    from opengrid.firmware.config import FirmwareConfig
    from opengrid.firmware.executor import FirmwareExecutor
    from opengrid.firmware.guardian_flow import PgFirmwareGuardianPort
    from opengrid.firmware.ingest import record_firmware_status
    from opengrid.firmware.model import CampaignState, JobState, WaveSpec
    from opengrid.firmware.repo import HubSelection, PgFirmwareRepo

    _cleanup(dsn)
    now = datetime.now(UTC)
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "INSERT INTO og.bank (bank_id, zone, kva_rating, feeder_id) VALUES ('fwit-bank', 'LZ_NORTH', 600, NULL)"
        )
        for hub in HUBS:
            conn.execute(
                "INSERT INTO og.hub (hub_id, bank_id, zone, e_kwh, r_kwh, p_kw, firmware_version, hardware_revision) "
                "VALUES (%s, 'fwit-bank', 'LZ_NORTH', 39.2, 7.84, 11, '1.4.2', 'revB')",
                (hub,),
            )
            conn.execute(
                "INSERT INTO og.hub_state (hub_id, soc_kwh, p_kw, health, last_seen_at) VALUES (%s, 30, 0, 'online', now())",
                (hub,),
            )
        conn.execute(
            "INSERT INTO og.firmware_catalogue (version, hardware_revision, sha256, release_note) "
            "VALUES ('9.1.0', 'revB', %s, 'fwit-image')",
            (SHA,),
        )
    try:
        repo = PgFirmwareRepo(pool)
        cfg = FirmwareConfig()
        catalogue = Catalogue.build((), await repo.catalogue_rows())
        assert catalogue.lookup("9.1.0", "revB") is not None

        created = await svc.create_campaign(
            repo,
            svc.CampaignRequest(
                name="fwit-campaign",
                target_version="9.1.0",
                selection=HubSelection(bank_ids=("fwit-bank",)),
                reason="integration",
                waves=WaveSpec(canary=1, size_pct=100),
                bank_max_concurrent_pct=100.0,
            ),
            proposer="op-a",
            catalogue=catalogue,
            cfg=cfg,
            now=now,
        )
        cid = created.campaign.campaign_id
        assert {j.hub_id for j in created.jobs} == set(HUBS)
        campaign = await svc.confirm_campaign(repo, cid, operator="op-a", now=now, ttl_s=60)
        assert campaign.state == CampaignState.APPROVED

        async def catalogue_source() -> Catalogue:
            return catalogue

        executor = FirmwareExecutor(repo, cfg, catalogue_source)
        result = await executor.step(datetime.now(UTC))
        assert result.requested == 1  # canary
        assert (await repo.get_campaign(cid)).state == CampaignState.RUNNING  # type: ignore[union-attr]

        port = PgFirmwareGuardianPort(pool)
        [pending] = await port.pending()
        facts = await port.hub_facts(pending.hub_id, lookahead_s=900, update_timeout_s=600)
        assert facts is not None and facts.online and not facts.safe_stopped and facts.bank_hub_count == 2
        grant = await port.grant(cid)
        assert grant is not None and grant.campaign_state == "RUNNING"
        assert await port.reserve(pending.command_id, pending.hub_id) == (1, 1)
        assert await port.reserve(pending.command_id, pending.hub_id) is None  # claimed once
        await port.mark_signed(pending.command_id, {"signed": True})
        await port.mark_published(pending.command_id)
        facts_after = await port.hub_facts(pending.hub_id, lookahead_s=900, update_timeout_s=600)
        assert facts_after is not None and facts_after.already_updating and facts_after.bank_in_flight == 1

        ts = datetime.now(UTC) + timedelta(seconds=1)
        stored = await record_firmware_status(
            pool,
            {"hub_id": pending.hub_id, "command_id": str(pending.command_id), "state": "DONE", "reason": None,
             "firmware_version": "9.1.0", "target_version": "9.1.0", "ts": ts.isoformat()},
        )  # fmt: skip
        assert stored
        await executor.step(datetime.now(UTC) + timedelta(seconds=2))
        jobs = {j.hub_id: j for j in await repo.jobs(cid)}
        assert jobs[pending.hub_id].state == JobState.SUCCEEDED
        events = [e.event for _, e in await repo.events(cid)]
        assert {"CAMPAIGN_CREATED", "CAMPAIGN_STARTED", "REQUESTED", "HUB_DONE", "SENT", "SUCCEEDED"} <= set(
            events
        )
        counts = await repo.fleet_counts()
        assert counts.bank_hubs["fwit-bank"] == 2
    finally:
        _cleanup(dsn)
