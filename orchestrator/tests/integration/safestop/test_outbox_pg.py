"""`og.stop_outbox` (migration 0035) against the workspace database: queued once per (stop_id, action),
drained oldest first, acknowledged rows never returned again. Rows are removed after the test."""

from __future__ import annotations

import os
from uuid import uuid4

import pytest
from psycopg_pool import AsyncConnectionPool

from opengrid.platform.config import load_config
from opengrid.platform.db import build_dsn, migrate_sync
from opengrid.safestop.pg_backend import PgStopEventBackend

pytestmark = [
    pytest.mark.asyncio,
    pytest.mark.skipif(not os.environ.get("OG_DB"), reason="needs the workspace database (tools/remote.ps1)"),
]


@pytest.fixture
async def pool():
    dsn = build_dsn(load_config())
    migrate_sync(dsn)
    async with AsyncConnectionPool(dsn, min_size=1, max_size=2, open=False) as opened:
        yield opened


async def test_outbox_queues_once_drains_in_order_and_forgets_acknowledged_rows(pool):
    backend = PgStopEventBackend(pool)
    first, second = uuid4(), uuid4()
    try:
        await backend.enqueue_publication(
            stop_id=first, action="ENGAGE", topic_suffix=f"stop/bank/b1/{first}", payload={"n": 1}
        )
        await backend.enqueue_publication(
            stop_id=second, action="ENGAGE", topic_suffix=f"stop/bank/b2/{second}", payload={"n": 2}
        )
        await backend.enqueue_publication(  # the same event again: a no-op
            stop_id=first, action="ENGAGE", topic_suffix=f"stop/bank/b1/{first}", payload={"n": 1}
        )
        mine = [
            e
            for e in await backend.pending_publications(limit=1000, max_attempts=5)
            if e.stop_id in (first, second)
        ]
        assert [e.stop_id for e in mine] == [first, second]
        assert mine[0].payload == {"n": 1}

        await backend.record_publish_failure(mine[0].seq, "broker down", permanent=False)
        await backend.mark_published(mine[0].seq)
        left = [
            e.stop_id
            for e in await backend.pending_publications(limit=1000, max_attempts=5)
            if e.stop_id in (first, second)
        ]
        assert left == [second]
    finally:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM og.stop_outbox WHERE stop_id = ANY(%s)", ([first, second],))


async def test_h5_atomic_write_priority_dead_letter_and_l2_record(pool):
    """H5 against the real schema: the stop_event row and its outbox entry are one transaction (a failing enqueue
    leaves neither); seq order within a scope, ENGAGE priority only across scopes; a dead-lettered entry is skipped and alerted once; the
    L2 record lookup reports whether the recorded ENGAGE has a publication."""
    from dataclasses import replace

    import psycopg

    from opengrid.safestop.backend import StopEventRow
    from opengrid.safestop.pg_backend import DEAD_LETTER_ALERT_RULE
    from opengrid.safestop.service import drain_order

    backend = PgStopEventBackend(pool)
    instruction, bank = uuid4(), f"bank-h5-{uuid4().hex[:6]}"
    release_id, engage_id, orphan_id = uuid4(), uuid4(), uuid4()
    ids = [release_id, engage_id, orphan_id]

    def row(stop_id, action, reason="x"):
        return StopEventRow(stop_id, "BANK", bank, action, "UTILITY", "utility:it", reason, None, uuid4().hex)

    try:
        await backend.record_and_enqueue(
            row(release_id, "RELEASE"), stop_id=release_id, topic_suffix=f"stop/bank/{bank}/{release_id}",
            payload={"n": "release"},
        )  # fmt: skip
        await backend.record_and_enqueue(
            row(engage_id, "ENGAGE", f"utility L2 BLOCK {instruction}"), stop_id=engage_id,
            topic_suffix=f"stop/bank/{bank}/{engage_id}", payload={"n": "engage"},
        )  # fmt: skip
        mine = [e for e in await backend.pending_publications(limit=1000, max_attempts=5) if e.stop_id in ids]
        # r3.4.1 MEDIUM-1: the backend returns seq order; within ONE scope the drain keeps it (the hub backstop
        # would drop a RELEASE published after the newer ENGAGE), ENGAGE priority applies only across scopes.
        assert [e.action for e in mine] == ["RELEASE", "ENGAGE"]
        assert [e.action for e in drain_order(mine)] == ["RELEASE", "ENGAGE"]
        older_release = replace(mine[0], seq=mine[1].seq + 1, topic_suffix=f"stop/bank/{bank}-a/{release_id}")
        newer_engage = replace(mine[1], seq=mine[1].seq + 2, topic_suffix=f"stop/bank/{bank}-b/{engage_id}")
        crossed = drain_order([older_release, newer_engage])
        assert [e.action for e in crossed] == ["ENGAGE", "RELEASE"]  # across scopes an ENGAGE head goes first

        record = await backend.l2_engage_record(instruction, bank)
        assert record is not None and record.stop_id == engage_id and record.has_publication

        # Atomic: a failing enqueue (a payload the column cannot take) leaves no stop_event row either.
        class _Unjsonable:
            pass

        with pytest.raises((TypeError, psycopg.Error)):
            await backend.record_and_enqueue(
                row(orphan_id, "ENGAGE"),
                stop_id=orphan_id,
                topic_suffix="stop/x",
                payload={"bad": _Unjsonable()},
            )
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("SELECT count(*) FROM og.stop_event WHERE stop_event_id = %s", (orphan_id,))
            assert (await cur.fetchone())[0] == 0

        engage = mine[1]
        assert await backend.record_publish_failure(engage.seq, "broker down", permanent=False) == 0
        assert await backend.record_publish_failure(engage.seq, "bad payload", permanent=True) == 1
        left = [e for e in await backend.pending_publications(limit=1000, max_attempts=1) if e.stop_id in ids]
        assert [e.action for e in left] == ["RELEASE"]  # dead-lettered at the cap: skipped
        await backend.raise_dead_letter_alert(engage, "bad payload")
        await backend.raise_dead_letter_alert(engage, "bad payload")  # once while open
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute(
                "SELECT count(*) FROM og.alert WHERE rule = %s AND cleared_at IS NULL AND detail ->> 'stop_id' = %s",
                (DEAD_LETTER_ALERT_RULE, str(engage_id)),
            )
            assert (await cur.fetchone())[0] == 1
    finally:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM og.stop_outbox WHERE stop_id = ANY(%s)", (ids,))
            await cur.execute("DELETE FROM og.stop_event WHERE stop_event_id = ANY(%s)", (ids,))
            await cur.execute(
                "DELETE FROM og.alert WHERE rule = %s AND detail ->> 'stop_id' = %s",
                ("ALR-STOP-PUBLISH-DEAD-LETTER", str(engage_id)),
            )


async def test_l1_newest_engage_time_and_the_superseded_release_alert(pool):
    """r3.4.2 review L-1 against the real schema: the newest ENGAGE time per scope, the once-while-open
    ALR-STOP-RELEASE-SUPERSEDED alert, and the guardian's re-hand query that skips a superseded release."""
    from datetime import UTC, datetime

    from opengrid.guardian.repo import PgStopReleasePort
    from opengrid.safestop.backend import StopEventRow
    from opengrid.safestop.pg_backend import SUPERSEDED_RELEASE_ALERT_RULE

    backend = PgStopEventBackend(pool)
    bank, engage_id, signature = f"bank-l1-{uuid4().hex[:6]}", uuid4(), f"sig-{uuid4().hex}"
    row = StopEventRow(engage_id, "BANK", bank, "ENGAGE", "UTILITY", "utility:it", "x", None, uuid4().hex)
    try:
        assert await backend.latest_engage_at("BANK", bank) is None
        await backend.record_and_enqueue(
            row, stop_id=engage_id, topic_suffix=f"stop/bank/{bank}/{engage_id}", payload={}
        )
        engaged = await backend.latest_engage_at("BANK", bank)
        assert engaged is not None and engaged <= datetime.now(UTC)
        kwargs = {"scope_kind": "BANK", "scope_ref": bank, "stop_id": engage_id, "signature": signature}
        assert await backend.raise_superseded_release_alert(**kwargs, engage_at=engaged) is True
        assert (
            await backend.raise_superseded_release_alert(**kwargs, engage_at=engaged) is False
        )  # once while open
        assert isinstance(await PgStopReleasePort(pool).unpublished_release_events(max_age_s=60.0), list)
    finally:
        async with pool.connection() as conn, conn.cursor() as cur:
            await cur.execute("DELETE FROM og.stop_outbox WHERE stop_id = %s", (engage_id,))
            await cur.execute("DELETE FROM og.stop_event WHERE stop_event_id = %s", (engage_id,))
            await cur.execute(
                "DELETE FROM og.alert WHERE rule = %s AND detail ->> 'stop_id' = %s",
                (SUPERSEDED_RELEASE_ALERT_RULE, str(engage_id)),
            )
