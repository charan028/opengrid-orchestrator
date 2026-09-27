"""D-35 against real Postgres (the disposable 5433 test cluster): the ERCOT AS instruction poller end to end --
ogsim's MMS simulator on the wire (in-process ASGI), the `ercot_mms` client, the shared call core
(`opengrid.calls`, `og.dispatch_call` / `og.as_deployment`), the Postgres trace store and health's alert
writer. Server only (`tools/remote.ps1`); every row it writes is removed afterwards."""

from __future__ import annotations

import os
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any
from uuid import UUID, uuid4

import httpx
import psycopg
import pytest
from psycopg_pool import AsyncConnectionPool

pytestmark = pytest.mark.skipif(
    not os.environ.get("OG_DB"), reason="requires the server environment (tools/remote.ps1)"
)
mms = pytest.importorskip("ogsim.protocols.ercot_mms")

RESOURCE = "OG_ESR_1"
TOLL_RESOURCE = "OG_ESR_2"
COMMITTED_KW = 500


@pytest.fixture(scope="module")
def dsn(server_config, _migrated) -> str:  # type: ignore[no-untyped-def]
    from opengrid.platform.db import build_dsn

    return str(build_dsn(server_config))


@pytest.fixture
async def pool(dsn: str) -> AsyncIterator[AsyncConnectionPool]:
    p = AsyncConnectionPool(dsn, min_size=1, max_size=4, open=False)
    await p.open()
    try:
        yield p
    finally:
        await p.close()


@dataclass
class Seed:
    ecrs_contract: UUID
    toll_contract: UUID
    award: UUID
    toll: UUID


def _insert_award(conn: psycopg.Connection[Any], contract_id: UUID, *, service: str, variant: str) -> UUID:
    rule_id, opportunity_id, obligation_id = uuid4(), uuid4(), uuid4()
    conn.execute(
        "INSERT INTO og.product_rule (product_rule_id, contract_id, product_code, min_qty_kw, increment_kw, block, "
        "duration_minutes, variable_kind) VALUES (%s, %s, %s, 100, 100, false, 60, 'SEMI_CONTINUOUS')",
        (rule_id, contract_id, variant),
    )
    conn.execute(
        "INSERT INTO og.opportunity (opportunity_id, contract_id, product_rule_id, window_start, window_end, "
        "requested_kw) VALUES (%s, %s, %s, now() - interval '1 hour', now() + interval '2 hours', %s)",
        (opportunity_id, contract_id, rule_id, COMMITTED_KW),
    )
    conn.execute(
        "INSERT INTO og.obligation (obligation_id, opportunity_id, contract_id, service_type, tier, window_start, "
        "window_end, committed_qty_kw, state) VALUES (%s, %s, %s, %s, 'T2', now() - interval '1 hour', "
        "now() + interval '2 hours', %s, 'COMMITTED')",
        (obligation_id, opportunity_id, contract_id, service, COMMITTED_KW),
    )
    return obligation_id


@pytest.fixture
def seed(dsn: str) -> Any:
    ecrs, toll = uuid4(), uuid4()
    with psycopg.connect(dsn, autocommit=True) as conn:
        # The toll contract needs its utility row (a fresh workspace DB has none); removed only if added here.
        added_utility = conn.execute(
            "INSERT INTO og.utility (utility_id, name, territory_zones, capacity_product, payment_basis, "
            "charging_tariff_kind, off_peak_rate_usd_per_kwh, tariff_ref) VALUES ('AUSTIN_ENERGY', 'it-aspoll', "
            "ARRAY['LZ_AEN'], 'TOLLING', 'USD_PER_KW_YEAR', 'TOU_OFF_PEAK', 0.05, 'it-aspoll') "
            "ON CONFLICT (utility_id) DO NOTHING RETURNING utility_id"
        ).fetchone()
        conn.execute(
            "INSERT INTO og.contract (contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, "
            "penalty_alpha, penalty_beta, penalty_theta) "
            "VALUES (%s, %s, 'ERCOT_AS', 'ECRS', 'T2', 'it-aspoll@1', now() - interval '1 day', 0.01, 0.25, 0.1)",
            (ecrs, uuid4()),
        )
        conn.execute(
            "INSERT INTO og.contract (contract_id, customer_id, service_type, variant, tier, profile_ref, start_at, "
            "market, utility_id, penalty_alpha, penalty_beta, penalty_theta) "
            "VALUES (%s, %s, 'REGULATED_CAPACITY', 'TOLLING', 'T2', 'it-aspoll-toll@1', now() - interval '1 day', "
            "'REGULATED', 'AUSTIN_ENERGY', 0.01, 0.25, 0.1)",
            (toll, uuid4()),
        )
        award = _insert_award(conn, ecrs, service="ERCOT_AS", variant="ECRS")
        toll_obligation = _insert_award(conn, toll, service="REGULATED_CAPACITY", variant="TOLLING")
    yield Seed(ecrs, toll, award, toll_obligation)
    with psycopg.connect(dsn, autocommit=True) as conn:
        ids = (award, toll_obligation)
        conn.execute("DELETE FROM og.dispatch_call WHERE obligation_id = ANY(%s)", (list(ids),))
        conn.execute("DELETE FROM og.dispatch_call WHERE principal = 'ercot:ercot_mms'")
        conn.execute("DELETE FROM og.as_deployment WHERE obligation_id = ANY(%s)", (list(ids),))
        conn.execute("DELETE FROM og.obligation WHERE obligation_id = ANY(%s)", (list(ids),))
        conn.execute("DELETE FROM og.opportunity WHERE contract_id = ANY(%s)", ([ecrs, toll],))
        conn.execute("DELETE FROM og.product_rule WHERE contract_id = ANY(%s)", ([ecrs, toll],))
        conn.execute("DELETE FROM og.contract WHERE contract_id = ANY(%s)", ([ecrs, toll],))
        conn.execute("DELETE FROM og.alert WHERE rule LIKE 'ALR-ERCOT-AS-%%'")
        conn.execute("DELETE FROM og.trace WHERE stream_id = 'ercot_as_poll'")
        if added_utility is not None:
            conn.execute("DELETE FROM og.utility WHERE utility_id = 'AUSTIN_ENERGY' AND name = 'it-aspoll'")


class _Cfg:
    def get(self, key: str, default: Any = None) -> Any:
        return default


def _poller(pool: AsyncConnectionPool, trace: Any, http: httpx.AsyncClient, seed: Seed) -> Any:
    from opengrid.contracts.as_deployment_poll import ErcotAsPoller, ErcotAsPollSettings
    from opengrid.contracts.as_deployment_poll_io import CoreCallGateway, PgAwardLookup, PgPollAlerts
    from opengrid.integrations.ercot_mms.client import ErcotMmsClient, ErcotMmsSettings

    mms_settings = {
        "endpoint": "http://mms.test/ews/",
        "qse_code": "QOPENGRID",
        "user_id": "it",
        "signing": "none",
    }
    settings = ErcotAsPollSettings.model_validate(
        {
            "enabled": True,
            "mms": mms_settings,
            "awards": {
                f"{RESOURCE}:ECRS": str(seed.ecrs_contract),
                f"{TOLL_RESOURCE}:ECRS": str(seed.toll_contract),
            },
        }
    )
    return ErcotAsPoller(
        source=ErcotMmsClient(ErcotMmsSettings.model_validate(mms_settings), client=http),
        calls=CoreCallGateway(pool, trace, _Cfg()),
        awards=PgAwardLookup(pool),
        alerts=PgPollAlerts(pool),
        trace=trace,
        settings=settings,
    )


def _rows(dsn: str, sql: str, params: tuple[Any, ...] = ()) -> list[tuple[Any, ...]]:
    with psycopg.connect(dsn) as conn:
        return list(conn.execute(sql, params).fetchall())


async def test_instructions_go_through_the_shared_core_and_are_answered(
    dsn: str, pool: AsyncConnectionPool, seed: Seed, tmp_path: Path
) -> None:
    from opengrid.trace.pg_backend import PgTraceBackend
    from opengrid.trace.store import TraceStore

    trace = TraceStore(PgTraceBackend(pool, journal_path=tmp_path / "journal.jsonl"))
    state = mms.MmsSimState()
    book = state.dispatch
    http = httpx.AsyncClient(
        transport=httpx.ASGITransport(app=mms.create_app(state)), base_url="http://mms.test"
    )
    now = datetime.now(UTC)
    end = now + timedelta(minutes=30)

    def deploy(resource: str, mw: float, **kw: Any) -> str:
        return str(
            book.publish(
                resource=resource, instruction_type="DEPLOY_AS", issued_at=now, as_type="ECRS", mw=mw,
                end=end, ramp_minutes=10, **kw,
            ).mrid
        )  # fmt: skip

    good = deploy(RESOURCE, 0.3)
    over = deploy(RESOURCE, 5.0)  # 5 MW against a 500 kW award
    bad = deploy(RESOURCE, 0.3, malformed_fields={"mw": "fifty"})
    unknown = deploy("OG_ESR_UNKNOWN", 0.3)
    toll = deploy(TOLL_RESOURCE, 0.3)  # mapped to the TOLLING contract: never an ERCOT_AS award
    zero = deploy(RESOURCE, 0.0)  # MW 0 must be malformed, never the full award (review, r3.4.2)

    poller = _poller(pool, trace, http, seed)
    try:
        assert await poller.poll_once(now=now) is True

        # Exactly one deployment, on the ERCOT_AS award, through the core (source ERCOT, origin ERCOT_POLL).
        deployments = _rows(
            dsn,
            "SELECT obligation_id, source, cancelled_at FROM og.as_deployment WHERE obligation_id = ANY(%s)",
            ([seed.award, seed.toll],),
        )
        assert deployments == [(seed.award, "ERCOT", None)]
        calls = _rows(
            dsn,
            "SELECT outcome, origin, requested_kw, idempotency_key FROM og.dispatch_call "
            "WHERE principal = 'ercot:ercot_mms' AND outcome = 'ACCEPTED'",
        )
        assert [(c[0], c[1], float(c[2]), c[3]) for c in calls] == [("ACCEPTED", "ERCOT_POLL", -300.0, good)]

        answers = {r["mrid"]: (r["response"], r["reason"] or "") for r in book.responses()}
        assert answers[good] == ("ACCEPTED", "")
        assert answers[over][0] == "REJECTED" and answers[over][1].startswith("409")
        assert answers[bad][0] == "REJECTED" and answers[bad][1].startswith("422")
        assert answers[unknown][0] == "REJECTED" and answers[unknown][1].startswith("404")
        assert answers[toll][0] == "REJECTED" and answers[toll][1].startswith("404")
        assert answers[zero][0] == "REJECTED" and answers[zero][1].startswith("422")

        refused = {
            r[0]
            for r in _rows(
                dsn, "SELECT detail ->> 'instruction_id' FROM og.alert WHERE rule = 'ALR-ERCOT-AS-REFUSED' "
                "AND cleared_at IS NULL",
            )
        }  # fmt: skip
        assert refused >= {over, bad, unknown, toll, zero}
        events = [
            (r[0], r[1])
            for r in _rows(
                dsn, "SELECT event_class, payload ->> 'origin' FROM og.trace WHERE stream_id = 'ercot_as_poll' "
                "ORDER BY seq",
            )
        ]  # fmt: skip
        assert ("AS_INSTRUCTION_ACCEPTED", "ERCOT_POLL") in events
        assert sum(1 for e in events if e[0] == "AS_INSTRUCTION_REFUSED") == 5

        # A duplicate delivery, then the same after a restart (a fresh poller): never a second deployment.
        book.force_duplicate(good)
        await poller.poll_once(now=now + timedelta(seconds=5))
        restarted = _poller(pool, trace, http, seed)
        book.force_duplicate(good)
        await restarted.poll_once(now=now + timedelta(seconds=10))
        assert len(_rows(dsn, "SELECT 1 FROM og.as_deployment WHERE obligation_id = %s", (seed.award,))) == 1

        # ERCOT recalls it: the deployment ends (cancelled, never deleted) and the recall is accepted.
        recall = book.publish(
            resource=RESOURCE, instruction_type="RECALL_AS", issued_at=datetime.now(UTC), as_type="ECRS",
            recall_of=good,
        )  # fmt: skip
        await restarted.poll_once(now=datetime.now(UTC))
        answers = {r["mrid"]: (r["response"], r["reason"] or "") for r in book.responses()}
        assert answers[recall.mrid] == ("ACCEPTED", "")
        assert not _rows(
            dsn,
            "SELECT 1 FROM og.as_deployment WHERE obligation_id = %s AND cancelled_at IS NULL AND end_at > now()",
            (seed.award,),
        )
        assert len(_rows(dsn, "SELECT 1 FROM og.as_deployment WHERE obligation_id = %s", (seed.award,))) == 1
    finally:
        await http.aclose()
