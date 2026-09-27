"""r3.4.3 PERF-OPT: `opengrid.fleet._classify` answers from an interval of instants over which the one hub-health
classifier (`health.rules.classify_hub_health`) was seen to give the same class at both ends. That is exact only
because, for fixed inputs, each class is a contiguous interval of `now` -- pinned here, so a change to the
classifier that breaks it fails loudly instead of silently mis-classifying hubs."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

from hypothesis import given
from hypothesis import strategies as st

from opengrid import fleet
from opengrid.health.model import HealthThresholds
from opengrid.health.rules import classify_hub_health

T0 = datetime(2026, 9, 27, 3, 0, tzinfo=UTC)


@given(
    stale_s=st.floats(min_value=0.0, max_value=600.0),
    offline_s=st.floats(min_value=0.0, max_value=600.0),
    fault=st.sampled_from([None, "", "BMS_FAULT"]),
    seen_offset_s=st.one_of(st.none(), st.floats(min_value=-900.0, max_value=900.0)),
    offsets=st.lists(st.integers(min_value=-1_000_000, max_value=1_000_000_000), min_size=3, max_size=12),
)
def test_each_health_class_is_a_contiguous_interval_of_now(stale_s, offline_s, fault, seen_offset_s, offsets):
    thresholds = HealthThresholds(hub_stale_s=stale_s, hub_offline_s=offline_s, telemetry_interval_s=2.0)
    last_seen = None if seen_offset_s is None else T0 + timedelta(seconds=seen_offset_s)
    instants = sorted(T0 + timedelta(microseconds=us) for us in offsets)
    classes = [
        classify_hub_health(fault_code=fault, last_seen_at=last_seen, now=t, thresholds=thresholds)
        for t in instants
    ]
    # Once a class is left it never comes back (so equal ends imply an equal middle).
    seen: list[str] = []
    for c in classes:
        if not seen or seen[-1] != c:
            assert c not in seen, (classes, instants)
            seen.append(c)


class _Runtime:
    def __init__(self, hub_id: str, last_seen_at: datetime | None, fault_code: str | None = None) -> None:
        self.hub_id, self.last_seen_at, self.fault_code = hub_id, last_seen_at, fault_code


def test_the_memo_answers_like_the_classifier_across_a_threshold() -> None:
    fleet._health_memo.clear()
    last_seen = T0
    hub = _Runtime("h-memo", last_seen)
    thresholds = fleet._thresholds
    for step_ms in range(0, 60_000, 250):
        now = T0 + timedelta(milliseconds=step_ms)
        expected = classify_hub_health(
            fault_code=None, last_seen_at=last_seen, now=now, thresholds=thresholds
        )
        assert fleet._classify(hub, now) == expected  # type: ignore[arg-type]
    # New telemetry (a new last_seen object) is never answered from the old entry.
    hub.last_seen_at = T0 + timedelta(seconds=59)
    now = T0 + timedelta(seconds=60)
    assert fleet._classify(hub, now) == classify_hub_health(  # type: ignore[arg-type]
        fault_code=None, last_seen_at=hub.last_seen_at, now=now, thresholds=thresholds
    )
    fleet._health_memo.clear()
