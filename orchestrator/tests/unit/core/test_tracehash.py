from hypothesis import given, settings
from hypothesis import strategies as st

from opengrid.core.tracehash import (
    ChainRecord,
    TraceRecordHeader,
    checkpoint_hash,
    compute_next_hash,
    payload_hash,
    record_hash,
    verify_chain,
)


def _build_chain(n: int) -> list[ChainRecord]:
    records = []
    prev = None
    for seq in range(n):
        payload = {"seq": seq, "note": f"event-{seq}"}
        _ph, h = compute_next_hash(
            stream_id="s1",
            seq=seq,
            decision_type="ADMISSION",
            event_class="ADMISSION",
            prev_hash=prev,
            payload=payload,
        )
        records.append(ChainRecord(seq, "ADMISSION", "ADMISSION", payload, prev, h))
        prev = h
    return records


def test_record_hash_deterministic():
    header = TraceRecordHeader("s1", 0, "ADMISSION", "ADMISSION", None, payload_hash({"a": 1}))
    assert record_hash(header) == record_hash(header)


def test_chain_verifies_when_intact():
    chain = _build_chain(10)
    result = verify_chain("s1", chain)
    assert result.ok


def test_chain_detects_tamper():
    chain = _build_chain(5)
    tampered = list(chain)
    tampered[2] = ChainRecord(
        tampered[2].seq,
        tampered[2].decision_type,
        tampered[2].event_class,
        {"seq": 2, "note": "TAMPERED"},
        tampered[2].prev_hash,
        tampered[2].hash,
    )
    result = verify_chain("s1", tampered)
    assert not result.ok
    assert result.broken_at_seq == 2


def test_chain_detects_broken_link():
    chain = _build_chain(5)
    broken = list(chain)
    broken[3] = ChainRecord(
        broken[3].seq,
        broken[3].decision_type,
        broken[3].event_class,
        broken[3].payload,
        "0" * 64,
        broken[3].hash,
    )
    result = verify_chain("s1", broken)
    assert not result.ok


def test_verify_after_prune_resumes_from_checkpoint():
    full_chain = _build_chain(20)
    # simulate pruning: keep only the tail, resume verification from the checkpointed head hash
    checkpoint_head = full_chain[9].hash
    tail = full_chain[10:]
    result = verify_chain("s1", tail, starting_prev_hash=checkpoint_head)
    assert result.ok


def test_checkpoint_hash_stable_under_key_order():
    heads_a = {"s1": {"seq": 5, "hash": "abc"}, "s2": {"seq": 1, "hash": "def"}}
    heads_b = {"s2": {"seq": 1, "hash": "def"}, "s1": {"seq": 5, "hash": "abc"}}
    assert checkpoint_hash(heads_a) == checkpoint_hash(heads_b)


@settings(deadline=None)  # building a chain hashes up to 50 records; CPU time, not a hang
@given(st.integers(min_value=1, max_value=50))
def test_chain_of_any_length_verifies(n):
    chain = _build_chain(n)
    assert verify_chain("s1", chain).ok


@settings(deadline=None)
@given(st.integers(min_value=2, max_value=50), st.integers(min_value=0))
def test_random_prune_point_still_verifies(n, prune_point_raw):
    chain = _build_chain(n)
    prune_point = prune_point_raw % n
    checkpoint_head = chain[prune_point].hash if prune_point >= 0 else None
    tail = chain[prune_point + 1 :]
    assert verify_chain("s1", tail, starting_prev_hash=checkpoint_head).ok
