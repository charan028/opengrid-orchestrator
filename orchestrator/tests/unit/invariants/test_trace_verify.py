"""Unit tests for `opengrid.invariants.trace_verify` (K11 scheduled verification): a broken chain is
detected and raises `ALR-TRACE-VERIFY-FAILED` exactly once; a clean chain advances the watermark and
clears any open alert. `TraceStore.verify` itself (the hashing) is exercised by `tests/unit/trace/
test_store.py` -- this only tests the scheduling/alerting wrapper, with `TraceStore` and the alert
functions faked."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import UTC, datetime

import pytest

from opengrid.core.tracehash import VerifyResult
from opengrid.invariants import trace_verify
from opengrid.invariants.trace_verify import ALR_TRACE_VERIFY_FAILED

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


@dataclass
class _FakeAlert:
    id: int
    rule: str
    detail: dict


class _FakeTraceStore:
    def __init__(self, results: dict[str, VerifyResult]) -> None:
        self._results = results
        self.calls: list[tuple[str, int]] = []

    async def verify(self, stream_id: str, *, from_seq: int) -> VerifyResult:
        self.calls.append((stream_id, from_seq))
        return self._results[stream_id]


@pytest.fixture(autouse=True)
def _patch_invariants_queries(monkeypatch: pytest.MonkeyPatch):
    class _FakeInvariantsQueries:
        def __init__(self) -> None:
            self.stream_ids: list[str] = []
            self.max_seq: dict[str, int] = {}

        async def fetch_trace_stream_ids(self, pool):
            return self.stream_ids

        async def fetch_trace_max_seq(self, pool, stream_id):
            return self.max_seq.get(stream_id)

    fake = _FakeInvariantsQueries()
    monkeypatch.setattr(trace_verify, "invariants_queries", fake)
    return fake


async def test_verify_new_segments_all_clean_advances_watermark(_patch_invariants_queries) -> None:
    _patch_invariants_queries.stream_ids = ["guardian_verdict", "commitment"]
    _patch_invariants_queries.max_seq = {"guardian_verdict": 41, "commitment": 9}
    store = _FakeTraceStore({"guardian_verdict": VerifyResult(True), "commitment": VerifyResult(True)})

    outcome = await trace_verify.verify_new_segments(pool=object(), trace_store=store, watermark={})

    assert outcome.checked_streams == 2
    assert outcome.failed_streams == ()
    assert outcome.new_watermark == {"guardian_verdict": 42, "commitment": 10}


async def test_verify_new_segments_detects_broken_chain_and_keeps_its_watermark(
    _patch_invariants_queries,
) -> None:
    _patch_invariants_queries.stream_ids = ["guardian_verdict"]
    _patch_invariants_queries.max_seq = {"guardian_verdict": 41}
    store = _FakeTraceStore(
        {"guardian_verdict": VerifyResult(False, broken_at_seq=17, reason="HASH_MISMATCH")}
    )

    outcome = await trace_verify.verify_new_segments(
        pool=object(), trace_store=store, watermark={"guardian_verdict": 5}
    )

    assert outcome.failed_streams == ("guardian_verdict",)
    # Watermark stays where it was (5), not advanced past the break -- the next run must find it again.
    assert outcome.new_watermark == {"guardian_verdict": 5}
    assert store.calls == [("guardian_verdict", 5)]


async def test_raise_or_clear_alert_raises_once_and_clears_on_recovery(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    open_alerts: list[_FakeAlert] = []
    raised: list[str] = []
    cleared: list[int] = []

    async def fake_fetch_open_alerts(pool):
        return open_alerts

    async def fake_raise_alert(pool, finding, *, opened_at):
        raised.append(finding.condition_key)
        alert = _FakeAlert(id=1, rule=finding.rule, detail=finding.detail)
        open_alerts.append(alert)
        return alert.id

    async def fake_clear_alert(pool, alert_id, *, cleared_at=None):
        cleared.append(alert_id)
        open_alerts[:] = [a for a in open_alerts if a.id != alert_id]

    def fake_condition_key_for(alert):
        return f"{alert.rule}:{','.join(alert.detail.get('failed_streams', []))}"

    monkeypatch.setattr(trace_verify, "fetch_open_alerts", fake_fetch_open_alerts)
    monkeypatch.setattr(trace_verify, "raise_alert", fake_raise_alert)
    monkeypatch.setattr(trace_verify, "clear_alert", fake_clear_alert)
    monkeypatch.setattr(trace_verify, "condition_key_for", fake_condition_key_for)

    failing_outcome = trace_verify.TraceVerifyOutcome(
        checked_streams=1, failed_streams=("guardian_verdict",), new_watermark={}
    )
    await trace_verify.raise_or_clear_alert(pool=object(), outcome=failing_outcome, now=NOW)
    assert raised == [f"{ALR_TRACE_VERIFY_FAILED}:guardian_verdict"]

    # Same condition again: no duplicate raise (TS-07-06-style de-duplication).
    await trace_verify.raise_or_clear_alert(pool=object(), outcome=failing_outcome, now=NOW)
    assert len(raised) == 1

    # Recovery: every stream verifies clean -> the open alert clears.
    clean_outcome = trace_verify.TraceVerifyOutcome(checked_streams=1, failed_streams=(), new_watermark={})
    await trace_verify.raise_or_clear_alert(pool=object(), outcome=clean_outcome, now=NOW)
    assert cleared == [1]
    assert open_alerts == []
