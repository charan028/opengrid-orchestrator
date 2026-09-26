"""Unit tests for `opengrid.invariants`'s orchestration (`run_once`/`run_due`/`run_trace_verify_once`)
with `invariants.queries` monkeypatched -- no real Postgres, mirroring
`tests/unit/health/test_init.py`'s pattern."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

import opengrid.invariants as invariants
from opengrid.invariants.models import (
    CHECK_ANCHOR_FRESHNESS,
    CHECK_AS_HOLD,
    CHECK_FLOW_LIMIT,
    CHECK_K1_RESERVE_BREACH,
    CHECK_K2_DOUBLE_SOLD,
    CHECK_K13_LOCK_VIOLATION,
    CHECK_K13_OUTAGE_GAP,
    CHECK_K13_RESTORE_LAG,
    CHECK_K15_TERRITORY,
    CHECK_ORPHAN_COMMITMENT,
    CHECK_ORPHAN_RESERVATION,
    CheckState,
)
from opengrid.platform.config import Config

pytestmark = pytest.mark.asyncio

NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)


class _FakeQueries:
    def __init__(self) -> None:
        # K1: a queue of (rows, new_cursor) results, one per loop iteration -- lets tests exercise the
        # multi-fetch loop. Defaults to a single empty fetch (nothing to find, caught up immediately).
        self.reserve_batches: list[tuple[list[tuple], tuple | None]] = [([], None)]
        self._reserve_batch_calls = 0

        self.reservation_agg_rows: list[tuple] = []
        self.bank_capability_inputs: list[tuple] = []

        self.lock_commitment_candidates: list[tuple] = []
        self.grant_cycle_series_by_obligation: dict = {}
        self.covering_trace_info_by_obligation: dict = {}  # obligation_id -> (earliest_at, cycle_ids)
        self.need_basis_obligation_ids: frozenset[str] = frozenset()
        self.shortfall_events_by_obligation: dict = {}
        self.measured_need_sample_by_obligation: dict = {}

        self.orphan_reservation_rows: list[tuple] = []
        self.orphan_commitment_rows: list[tuple] = []

        self.territory_candidates: list[tuple] = []
        self.as_hold_candidates: list[tuple] = []
        self.flow_limit_candidates: list[tuple] = []
        self.latest_anchor_published_at = None

        self.states: dict[str, CheckState] = {}
        self.upserts: list[dict] = []
        self.inserted_violations: dict[str, list] = {}
        # dedupe_keys already "persisted" from a prior call, per check -- insert_violations skips them,
        # mirroring the real ON CONFLICT DO NOTHING behaviour.
        self._seen_dedupe_keys: dict[str, set[str]] = {}

    async def get_check_state(self, pool, check_name):
        return self.states.get(check_name, CheckState.empty(check_name))

    async def fetch_reserve_breach_candidates(self, pool, *, since_ts, since_hub_id, upper_bound, limit=5000):
        idx = min(self._reserve_batch_calls, len(self.reserve_batches) - 1)
        self._reserve_batch_calls += 1
        return self.reserve_batches[idx]

    async def fetch_reservation_aggregates(self, pool, *, horizon_start):
        return self.reservation_agg_rows

    async def fetch_bank_capability_inputs(self, pool):
        return self.bank_capability_inputs

    async def fetch_lock_commitment_candidates(self, pool, *, since, now, limit=5000):
        return (
            self.lock_commitment_candidates,
            (self.lock_commitment_candidates[-1][2] if self.lock_commitment_candidates else None),
        )

    async def fetch_grant_cycle_series(self, pool, *, obligation_id, window_start, window_end):
        return self.grant_cycle_series_by_obligation.get(obligation_id, [])

    async def fetch_covering_trace_info(self, pool, *, obligation_id, window_start, window_end):
        return self.covering_trace_info_by_obligation.get(obligation_id, (None, frozenset()))

    async def fetch_need_basis_obligation_ids(self, pool, obligation_ids):
        return frozenset(str(oid) for oid in obligation_ids) & self.need_basis_obligation_ids

    async def fetch_shortfall_events(self, pool, *, obligation_id, window_start, window_end):
        return self.shortfall_events_by_obligation.get(obligation_id, [])

    async def fetch_measured_need_sample(self, pool, *, obligation_id, at):
        return self.measured_need_sample_by_obligation.get(obligation_id)

    async def fetch_orphan_reservations(self, pool, *, limit=5000):
        return self.orphan_reservation_rows

    async def fetch_orphan_commitments(self, pool, *, limit=5000):
        return self.orphan_commitment_rows

    async def fetch_territory_candidates(self, pool, *, since, now, limit=5000):
        return self.territory_candidates, (
            self.territory_candidates[-1][6] if self.territory_candidates else None
        )

    async def fetch_as_hold_candidates(self, pool, *, now):
        return self.as_hold_candidates

    async def fetch_flow_limit_candidates(self, pool):
        return self.flow_limit_candidates

    async def fetch_latest_anchor_published_at(self, pool):
        return self.latest_anchor_published_at

    async def insert_violations(self, pool, check_name, violations):
        seen = self._seen_dedupe_keys.setdefault(check_name, set())
        newly_inserted = [v for v in violations if v.dedupe_key not in seen]
        seen.update(v.dedupe_key for v in newly_inserted)
        self.inserted_violations.setdefault(check_name, []).extend(newly_inserted)
        return newly_inserted

    async def upsert_check_state(
        self, pool, check_name, *, ran_at, run_ms, violation_count, total_violations, watermark
    ):
        self.upserts.append(
            {
                "check_name": check_name,
                "violation_count": violation_count,
                "total_violations": total_violations,
                "watermark": watermark,
            }
        )
        self.states[check_name] = CheckState(
            check_name=check_name,
            last_run_at=ran_at,
            last_run_ms=run_ms,
            last_violations=violation_count,
            total_violations=total_violations,
            watermark=watermark,
        )


@pytest.fixture(autouse=True)
def _reset_invariants_module(monkeypatch: pytest.MonkeyPatch) -> None:
    invariants.configure(pool=object(), cfg=Config({}))  # type: ignore[arg-type]
    monkeypatch.setattr(invariants, "_now", lambda: NOW)
    yield
    invariants._pool = None


@pytest.fixture
def fake_queries(monkeypatch: pytest.MonkeyPatch) -> _FakeQueries:
    fake = _FakeQueries()
    monkeypatch.setattr(invariants, "queries", fake)
    return fake


async def test_run_once_reports_zero_on_clean_data(fake_queries: _FakeQueries) -> None:
    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K1_RESERVE_BREACH].count == 0
    assert outcomes[CHECK_K2_DOUBLE_SOLD].count == 0
    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 0
    assert outcomes[CHECK_K13_OUTAGE_GAP].count == 0
    assert outcomes[CHECK_K13_RESTORE_LAG].count == 0
    assert outcomes[CHECK_ORPHAN_RESERVATION].count == 0
    assert outcomes[CHECK_ORPHAN_COMMITMENT].count == 0
    assert outcomes[CHECK_K15_TERRITORY].count == 0
    assert outcomes[CHECK_AS_HOLD].count == 0
    assert outcomes[CHECK_FLOW_LIMIT].count == 0
    assert outcomes[CHECK_ANCHOR_FRESHNESS].count == 1  # never anchored yet -- reported stale, not skipped
    assert len(fake_queries.upserts) == 11  # every check persisted its result


async def test_run_once_detects_seeded_reserve_breach(fake_queries: _FakeQueries) -> None:
    fake_queries.reserve_batches = [([("hub-1", NOW, 1.0, -5.0, 2.0)], (NOW, "hub-1"))]

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K1_RESERVE_BREACH].count == 1


async def test_run_once_k1_loops_until_a_short_batch_catches_up(
    fake_queries: _FakeQueries, monkeypatch: pytest.MonkeyPatch
) -> None:
    """K1's catch-up-loop fix: a full-sized first batch must trigger a second fetch, not stop after one
    round. Force the "full batch" threshold down to 1 row so a 1-row batch counts as "more may follow"."""
    monkeypatch.setattr(invariants, "_K1_BATCH_LIMIT", 1)
    fake_queries.reserve_batches = [
        ([("hub-1", NOW, 1.0, -5.0, 2.0)], (NOW, "hub-1")),  # full batch (1 row) -> loop again
        ([], None),  # caught up
    ]

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K1_RESERVE_BREACH].count == 1
    assert fake_queries._reserve_batch_calls == 2


async def test_run_once_detects_seeded_double_sold_using_true_capability(fake_queries: _FakeQueries) -> None:
    fake_queries.reservation_agg_rows = [("bank-000", NOW, NOW + timedelta(minutes=15), 700.0)]
    # A hub row that makes compute_bank_capabilities_kw resolve bank-000's true capability to 600 kW (enough
    # stored energy to sustain its rating for the whole interval: capability is energy-limited, ES03-S05).
    # 30 dual-unit homes (20 kW each, the G-02 unit cap) = 600 kW.
    fake_queries.bank_capability_inputs = [
        ("bank-000", 10_000.0, 0.0, 1000.0, 7.84, 20.0, 0.9487, 0.9487, 1000.0, "online", 2),
    ] * 30

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K2_DOUBLE_SOLD].count == 1
    assert outcomes[CHECK_K2_DOUBLE_SOLD].total_magnitude == pytest.approx(25.0)


async def test_run_once_detects_seeded_lock_violation(fake_queries: _FakeQueries) -> None:
    # A window no wider than the sample coverage: isolates "grants flowing but low" (LOCK_VIOLATION)
    # from the separate gap-detection path (OUTAGE_GAP) -- a wide window with only one sample would
    # itself form a trailing gap and misclassify.
    interval_start = NOW
    interval_end = NOW + timedelta(seconds=6)
    fake_queries.lock_commitment_candidates = [("ob-1", interval_start, interval_end, 100.0)]
    fake_queries.grant_cycle_series_by_obligation["ob-1"] = [
        (interval_start + timedelta(seconds=2), 10.0, "cyc-1"),
        (interval_start + timedelta(seconds=4), 10.0, "cyc-2"),
    ]
    # No covering trace registered for "ob-1" -- fetch_covering_trace_info defaults to (None, frozenset()).

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 1
    assert outcomes[CHECK_K13_OUTAGE_GAP].count == 0


async def test_run_once_clean_when_lock_dip_is_covered_by_timestamp(fake_queries: _FakeQueries) -> None:
    # Narrow window (no wider than the samples' own coverage): isolates the covering-by-timestamp
    # question from find_dip's separate gap-detection path.
    interval_start = NOW
    interval_end = interval_start + timedelta(seconds=6)
    dip_at = interval_start + timedelta(seconds=2)
    fake_queries.lock_commitment_candidates = [("ob-1", interval_start, interval_end, 100.0)]
    fake_queries.grant_cycle_series_by_obligation["ob-1"] = [
        (dip_at, 10.0, "cyc-1"),
        (interval_start + timedelta(seconds=4), 10.0, "cyc-2"),
    ]
    fake_queries.covering_trace_info_by_obligation["ob-1"] = (dip_at, frozenset())  # covered at the dip

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 0
    assert outcomes[CHECK_K13_OUTAGE_GAP].count == 0


async def test_run_once_clean_when_lock_dip_is_covered_by_same_cycle_id(fake_queries: _FakeQueries) -> None:
    """The write-order fix: a covering trace timestamped AFTER the dip still covers it when they share
    the same allocator cycle_id."""
    interval_start = NOW
    interval_end = interval_start + timedelta(seconds=6)
    dip_at = interval_start + timedelta(seconds=2)
    fake_queries.lock_commitment_candidates = [("ob-1", interval_start, interval_end, 100.0)]
    fake_queries.grant_cycle_series_by_obligation["ob-1"] = [
        (dip_at, 10.0, "cyc-1"),
        (interval_start + timedelta(seconds=4), 10.0, "cyc-2"),
    ]
    covering_at = dip_at + timedelta(milliseconds=5)  # traced moments AFTER the grant, same cycle
    fake_queries.covering_trace_info_by_obligation["ob-1"] = (covering_at, frozenset({"cyc-1"}))

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 0
    assert outcomes[CHECK_K13_OUTAGE_GAP].count == 0


async def test_run_once_classifies_a_grant_activity_gap_as_outage_not_lock_violation(
    fake_queries: _FakeQueries,
) -> None:
    interval_start = NOW
    interval_end = NOW + timedelta(minutes=15)
    fake_queries.lock_commitment_candidates = [("ob-1", interval_start, interval_end, 100.0)]
    fake_queries.grant_cycle_series_by_obligation["ob-1"] = []  # total silence -> a gap dip

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 0
    assert outcomes[CHECK_K13_OUTAGE_GAP].count == 1


async def test_run_once_need_basis_obligation_dip_is_never_flagged(fake_queries: _FakeQueries) -> None:
    """Owner decision 2026-09-26: a MEASURED_FEEDBACK (DATA_CENTER/PIPELINE_AC) obligation's committed
    kW is a reserved maximum -- total silence (zero measured need) is compliant, not an outage gap."""
    interval_start = NOW
    interval_end = NOW + timedelta(minutes=15)
    fake_queries.lock_commitment_candidates = [("ob-dc", interval_start, interval_end, 100.0)]
    fake_queries.grant_cycle_series_by_obligation["ob-dc"] = []  # zero grants: the customer needed none
    fake_queries.need_basis_obligation_ids = frozenset({"ob-dc"})

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_LOCK_VIOLATION].count == 0
    assert outcomes[CHECK_K13_OUTAGE_GAP].count == 0


async def test_run_once_need_basis_flags_unmet_measured_need(fake_queries: _FakeQueries) -> None:
    """K13 need-basis violation (b): a real measured need with no delivery and no override IS flagged."""
    interval_start = NOW
    interval_end = NOW + timedelta(minutes=15)
    fake_queries.lock_commitment_candidates = [("ob-dc", interval_start, interval_end, 100.0)]
    fake_queries.grant_cycle_series_by_obligation["ob-dc"] = []  # total silence
    fake_queries.need_basis_obligation_ids = frozenset({"ob-dc"})
    from opengrid.invariants.checks import MeasuredNeedSample

    fake_queries.measured_need_sample_by_obligation["ob-dc"] = MeasuredNeedSample(
        kind="site_meter", field="p_kw", value=50.0, limit=None, quality="GOOD"
    )

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_OUTAGE_GAP].count == 1  # total silence -> classified via the gap path


async def test_run_once_restore_lag_flagged_when_not_restored_in_time(fake_queries: _FakeQueries) -> None:
    from opengrid.invariants.checks import ShortfallEvent

    interval_start = NOW
    interval_end = NOW + timedelta(minutes=15)
    cleared_at = interval_start + timedelta(minutes=1)
    fake_queries.lock_commitment_candidates = [("ob-1", interval_start, interval_end, 100.0)]
    fake_queries.grant_cycle_series_by_obligation["ob-1"] = [
        (cleared_at + timedelta(seconds=2), 60.0, "cyc-1"),
        (cleared_at + timedelta(seconds=4), 60.0, "cyc-2"),
    ]
    fake_queries.shortfall_events_by_obligation["ob-1"] = [
        ShortfallEvent(at=interval_start, shortfall_kw=40.0, cycle_id="cyc-0"),
        ShortfallEvent(at=cleared_at, shortfall_kw=0.0, cycle_id="cyc-1b"),
    ]

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K13_RESTORE_LAG].count == 1


async def test_run_once_detects_seeded_orphans(fake_queries: _FakeQueries) -> None:
    fake_queries.orphan_reservation_rows = [("res-1", "ob-1", "bank-000", NOW)]
    fake_queries.orphan_commitment_rows = [("com-1", "ob-2", NOW, "REJECTED")]

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_ORPHAN_RESERVATION].count == 1
    assert outcomes[CHECK_ORPHAN_COMMITMENT].count == 1


async def test_run_once_carries_forward_watermark_across_runs(fake_queries: _FakeQueries) -> None:
    fake_queries.reserve_batches = [([("hub-1", NOW, 1.0, -5.0, 2.0)], (NOW, "hub-1"))]
    await invariants.run_once()
    first_watermark = fake_queries.upserts[0]["watermark"]
    assert first_watermark == {"since_ts": NOW.isoformat(), "since_hub_id": "hub-1"}

    # Second run: the same watermark, no new rows -- total_violations must not double-count.
    fake_queries.reserve_batches = [([], None)]
    fake_queries._reserve_batch_calls = 0
    await invariants.run_once()
    second_upsert = next(u for u in fake_queries.upserts[11:] if u["check_name"] == CHECK_K1_RESERVE_BREACH)
    assert second_upsert["violation_count"] == 0
    assert second_upsert["total_violations"] == 1  # unchanged from the first run's total


async def test_run_once_does_not_recount_a_persisting_violation_across_runs(
    fake_queries: _FakeQueries,
) -> None:
    """The idempotency fix (#5b): the SAME still-true K2 over-sale, re-detected on every run, must be
    counted into the running total once, not once per run."""
    fake_queries.reservation_agg_rows = [("bank-000", NOW, NOW + timedelta(minutes=15), 700.0)]
    # 30 dual-unit homes (20 kW each, the G-02 unit cap) = 600 kW.
    fake_queries.bank_capability_inputs = [
        ("bank-000", 10_000.0, 0.0, 1000.0, 7.84, 20.0, 0.9487, 0.9487, 1000.0, "online", 2),
    ] * 30

    await invariants.run_once()
    await invariants.run_once()
    await invariants.run_once()

    k2_upserts = [u for u in fake_queries.upserts if u["check_name"] == CHECK_K2_DOUBLE_SOLD]
    assert len(k2_upserts) == 3
    # Every run still reports it as "currently found" (violation_count == 1)...
    assert all(u["violation_count"] == 1 for u in k2_upserts)
    # ...but the cumulative total only grew on the first run, not all three.
    assert k2_upserts[0]["total_violations"] == pytest.approx(25.0)
    assert k2_upserts[1]["total_violations"] == pytest.approx(25.0)
    assert k2_upserts[2]["total_violations"] == pytest.approx(25.0)


async def test_run_due_is_a_noop_when_not_configured() -> None:
    invariants._pool = None
    invariants._cadence = None
    invariants._trace_cadence = None
    await invariants.run_due()  # must not raise


async def test_run_due_swallows_check_failures(monkeypatch: pytest.MonkeyPatch) -> None:
    async def _boom() -> None:
        raise RuntimeError("boom")

    monkeypatch.setattr(invariants, "run_once", _boom)
    monkeypatch.setattr(invariants, "run_trace_verify_once", _boom)

    await invariants.run_due()  # must not raise, even though both checks are due and both fail


# --- K15 territory / AS-hold / flow-limit / K11 anchor freshness --------------------------------------


async def test_run_once_detects_seeded_territory_violation(fake_queries: _FakeQueries) -> None:
    fake_queries.territory_candidates = [
        ("grant-1", "ob-1", "bank-000", "LZ_HOUSTON", "AUSTIN_ENERGY", ("LZ_AEN",), NOW),
    ]

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_K15_TERRITORY].count == 1


async def test_run_once_detects_seeded_as_hold_violation(fake_queries: _FakeQueries) -> None:
    fake_queries.as_hold_candidates = [("dep-1", "ob-1", 100.0, 240, 250.0)]  # needs 400 kWh, has 250

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_AS_HOLD].count == 1


async def test_run_once_detects_seeded_flow_limit_violation(fake_queries: _FakeQueries) -> None:
    fake_queries.flow_limit_candidates = [("home_meter", "hub-1", -12.0, None, 10.0)]

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_FLOW_LIMIT].count == 1


async def test_run_once_anchor_freshness_clean_when_recently_published(fake_queries: _FakeQueries) -> None:
    fake_queries.latest_anchor_published_at = NOW - timedelta(minutes=5)  # well within default tolerance

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_ANCHOR_FRESHNESS].count == 0


async def test_run_once_anchor_freshness_flags_stale_publish(fake_queries: _FakeQueries) -> None:
    invariants._anchor_interval_s = 900.0
    fake_queries.latest_anchor_published_at = NOW - timedelta(hours=1)  # well past 2x900s tolerance

    outcomes = await invariants.run_once()

    assert outcomes[CHECK_ANCHOR_FRESHNESS].count == 1


async def test_run_due_also_publishes_an_anchor_when_due(monkeypatch: pytest.MonkeyPatch) -> None:
    calls = {"n": 0}

    async def _fake_publish() -> None:
        calls["n"] += 1

    monkeypatch.setattr(invariants, "run_anchor_publish_once", _fake_publish)
    invariants._anchor_cadence.due = lambda: True  # type: ignore[union-attr]
    invariants._cadence.due = lambda: False  # type: ignore[union-attr]
    invariants._trace_cadence.due = lambda: False  # type: ignore[union-attr]

    await invariants.run_due()

    assert calls["n"] == 1


async def test_run_anchor_publish_once_delegates_to_anchoring(monkeypatch: pytest.MonkeyPatch) -> None:
    captured = {}

    async def _fake_publish_anchor(pool, trace_store, cfg):
        captured["called"] = True
        return "sentinel-result"

    monkeypatch.setattr(invariants.anchoring, "publish_anchor", _fake_publish_anchor)

    result = await invariants.run_anchor_publish_once()

    assert captured["called"] is True
    assert result == "sentinel-result"
