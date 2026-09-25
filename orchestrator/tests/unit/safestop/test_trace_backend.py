from __future__ import annotations

from datetime import UTC, datetime
from uuid import uuid4

from opengrid.safestop.trace_backend import DEFAULT_RETENTION_DAYS, PgSafestopTraceBackend

from ._fake_pool import FakePool


async def test_last_head_empty_stream_returns_minus_one_and_none():
    pool = FakePool(fetchone_result=None)
    backend = PgSafestopTraceBackend(pool)  # type: ignore[arg-type]
    assert await backend.last_head("safestop") == (-1, None)


async def test_last_head_returns_seq_and_hash():
    pool = FakePool(fetchone_result=(3, "deadbeef"))
    backend = PgSafestopTraceBackend(pool)  # type: ignore[arg-type]
    assert await backend.last_head("safestop") == (3, "deadbeef")


async def test_insert_trace_row_sends_expected_params():
    pool = FakePool()
    backend = PgSafestopTraceBackend(pool)  # type: ignore[arg-type]
    trace_id = uuid4()

    await backend.insert_trace_row(
        trace_id=trace_id,
        stream_id="safestop",
        seq=0,
        decision_type="SAFE_STOP",
        event_class="SAFE_STOP",
        payload={"a": 1},
        reason_codes=["SAFE_STOP_ENGAGE"],
        prev_hash=None,
        record_hash="abc123",
        created_at=datetime.now(UTC),
    )

    assert len(pool.executed) == 1
    keyword, params = pool.executed[0]
    assert keyword == "INSERT"
    assert params["trace_id"] == trace_id
    assert params["hash"] == "abc123"


async def test_exists_preimage_true_and_false():
    present = FakePool(fetchone_result=(1,))
    absent = FakePool(fetchone_result=None)
    backend_present = PgSafestopTraceBackend(present)  # type: ignore[arg-type]
    backend_absent = PgSafestopTraceBackend(absent)  # type: ignore[arg-type]

    assert await backend_present.exists_preimage(uuid4()) is True
    assert await backend_absent.exists_preimage(uuid4()) is False


async def test_fetch_range_maps_rows_to_chain_records():
    rows = [(0, "SAFE_STOP", "SAFE_STOP", {"a": 1}, None, "hash0")]
    pool = FakePool(fetchall_result=rows)
    backend = PgSafestopTraceBackend(pool)  # type: ignore[arg-type]

    records = await backend.fetch_range("safestop", from_seq=0)

    assert len(records) == 1
    assert records[0].seq == 0
    assert records[0].hash == "hash0"


async def test_stream_ids_returns_list_of_strings():
    pool = FakePool(fetchall_result=[("safestop",), ("guardian",)])
    backend = PgSafestopTraceBackend(pool)  # type: ignore[arg-type]
    assert await backend.stream_ids() == ["safestop", "guardian"]


async def test_insert_checkpoint_sends_expected_params():
    pool = FakePool()
    backend = PgSafestopTraceBackend(pool)  # type: ignore[arg-type]
    checkpoint_id = uuid4()

    await backend.insert_checkpoint(
        checkpoint_id=checkpoint_id,
        checkpoint_at=datetime.now(UTC),
        stream_heads={"safestop": {"seq": 0, "hash": "h"}},
        checkpoint_hash_hex="chkhash",
    )

    assert len(pool.executed) == 1
    keyword, params = pool.executed[0]
    assert keyword == "INSERT"
    assert params["checkpoint_id"] == checkpoint_id


async def test_prune_before_returns_rowcount():
    pool = FakePool(delete_rowcount=7)
    backend = PgSafestopTraceBackend(pool)  # type: ignore[arg-type]
    deleted = await backend.prune_before("safestop", keep_from_seq=100)
    assert deleted == 7


async def test_retention_days_for_known_and_unknown_class():
    known = FakePool(fetchone_result=(90,))
    unknown = FakePool(fetchone_result=None)
    backend_known = PgSafestopTraceBackend(known)  # type: ignore[arg-type]
    backend_unknown = PgSafestopTraceBackend(unknown)  # type: ignore[arg-type]

    assert await backend_known.retention_days_for("SAFE_STOP") == 90
    assert await backend_unknown.retention_days_for("UNKNOWN_CLASS") == DEFAULT_RETENTION_DAYS
