"""Unit tests for the expectations table and the pure evaluator, with hand-built observation timelines."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from typing import Any

from expectations import (
    ALL_PROCESSES,
    BY_PROCESS,
    EXPECTATIONS,
    INFRA,
    Observation,
    evaluate,
    markdown_table,
)

T0 = datetime(2026, 9, 25, 12, 0, tzinfo=UTC)


def health(
    *,
    at_s: float,
    down: set[str] = frozenset(),  # type: ignore[assignment]
    stuck: set[str] = frozenset(),  # type: ignore[assignment]
    modes: list[str] | None = None,
    alerts: list[str] | None = None,
    counters: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """A health payload at `T0 + at_s`; `stuck` processes keep their T0 heartbeat ts."""
    ts_now = (T0 + timedelta(seconds=at_s)).isoformat()
    return {
        "processes": {
            p: {
                "pid": 1,
                "ts": T0.isoformat() if p in stuck or p in down else ts_now,
                "status": "down" if p in down else "ok",
            }
            for p in ALL_PROCESSES
        },
        "degraded_modes": [] if modes is None else modes,
        "alerts": [{"rule": r, "severity": "critical", "summary": r} for r in (alerts or [])],
        "hub_health_counts": {"online": 2000},
        **{"reserve_breaches": 0, "double_sold_kwh": 0, **(counters or {})},
    }


def engine_kill_timeline(*, extra_down: set[str] = frozenset(), counters: dict[str, Any] | None = None):  # type: ignore[assignment]
    down = {"engine", *extra_down}
    return [
        Observation("before", 0.0, health(at_s=0)),
        Observation("outage", 2.0, health(at_s=2)),
        Observation(
            "outage",
            18.0,
            health(at_s=18, down=down, modes=["HOLD_LOCAL_AUTONOMY"], alerts=["ALR-PROCESS-DOWN"]),
        ),
        Observation(
            "outage",
            28.0,
            health(
                at_s=28,
                down=down,
                modes=["HOLD_LOCAL_AUTONOMY"],
                alerts=["ALR-PROCESS-DOWN"],
                counters=counters,
            ),
        ),
        Observation("recovery", 2.0, health(at_s=30, down=down, modes=["HOLD_LOCAL_AUTONOMY"])),
        Observation("recovery", 8.0, health(at_s=36)),
    ]


def test_table_covers_all_seven_processes_plus_infra() -> None:
    assert {e.process for e in EXPECTATIONS} == set(ALL_PROCESSES) | set(INFRA)
    assert BY_PROCESS["engine"].degraded_modes == {"HOLD_LOCAL_AUTONOMY"}
    assert BY_PROCESS["guardian"].degraded_modes == {"HOLD"}
    assert (
        "safestop" in BY_PROCESS["engine"].must_stay_ok and "safestop" in BY_PROCESS["guardian"].must_stay_ok
    )
    assert BY_PROCESS["feeds"].eventual_modes == {"NO_NEW_COMMITMENTS"}
    assert BY_PROCESS["api"].health_unreachable and BY_PROCESS["settle"].health_frozen
    assert all(e.infra for e in EXPECTATIONS if e.process in INFRA)
    assert markdown_table().count("\n") == len(EXPECTATIONS) + 1


def test_engine_kill_correct_timeline_passes() -> None:
    findings = evaluate(engine_kill_timeline(), BY_PROCESS["engine"])
    assert findings and all(f.passed for f in findings), [f for f in findings if not f.passed]
    assert {f.check for f in findings} >= {
        "processes.engine.status == down",
        "degraded_modes contains ['HOLD_LOCAL_AUTONOMY']",
        "recovery",
        "independent processes kept heartbeating",
    }


def test_engine_kill_with_safestop_also_down_fails_k8() -> None:
    findings = evaluate(engine_kill_timeline(extra_down={"safestop"}), BY_PROCESS["engine"])
    failed = {f.check: f.detail for f in findings if not f.passed}
    assert any("stay ok" in c for c in failed), failed
    assert any("safestop" in d for d in failed.values())


def test_engine_kill_reserve_breach_fails_a10() -> None:
    findings = evaluate(engine_kill_timeline(counters={"reserve_breaches": 1}), BY_PROCESS["engine"])
    failed = {f.check: f.detail for f in findings if not f.passed}
    assert failed == {"['reserve_breaches', 'double_sold_kwh'] stay 0": "reserve_breaches=1"}


def test_engine_kill_too_slow_or_wrong_mode_fails() -> None:
    late = [
        Observation("before", 0.0, health(at_s=0)),
        Observation(
            "outage", 45.0, health(at_s=45, down={"engine"}, modes=["HOLD"], alerts=["ALR-PROCESS-DOWN"])
        ),
        Observation("recovery", 2.0, health(at_s=50)),
    ]
    failed = {f.check for f in evaluate(late, BY_PROCESS["engine"]) if not f.passed}
    assert "processes.engine.status == down" in failed  # 45 s > 30 s
    assert "degraded_modes contains ['HOLD_LOCAL_AUTONOMY']" in failed


def test_missing_degraded_modes_key_is_an_explicit_failure() -> None:
    tl = engine_kill_timeline()
    for o in tl:
        if o.health:
            o.health.pop("degraded_modes")
    findings = {f.check: f for f in evaluate(tl, BY_PROCESS["engine"])}
    f = findings["degraded_modes contains ['HOLD_LOCAL_AUTONOMY']"]
    assert not f.passed and "missing from the health payload" in f.detail


def test_guardian_kill_passes_and_engine_must_keep_heartbeating() -> None:
    def tl(stuck: set[str]):
        return [
            Observation("before", 0.0, health(at_s=0)),
            Observation(
                "outage",
                18.0,
                health(at_s=18, down={"guardian"}, stuck=stuck, modes=["HOLD"], alerts=["ALR-PROCESS-DOWN"]),
            ),
            Observation(
                "outage",
                28.0,
                health(at_s=28, down={"guardian"}, stuck=stuck, modes=["HOLD"], alerts=["ALR-PROCESS-DOWN"]),
            ),
            Observation("recovery", 6.0, health(at_s=34)),
        ]

    assert all(f.passed for f in evaluate(tl(set()), BY_PROCESS["guardian"]))
    failed = {f.check: f.detail for f in evaluate(tl({"engine"}), BY_PROCESS["guardian"]) if not f.passed}
    assert failed == {"independent processes kept heartbeating": "ts not advancing: ['engine']"}


def test_api_kill_expects_unreachable_then_recovery() -> None:
    good = [
        Observation("before", 0.0, health(at_s=0)),
        Observation("outage", 2.0, error="connection refused"),
        Observation("outage", 12.0, error="connection refused"),
        Observation("recovery", 4.0, error="connection refused"),
        Observation("recovery", 8.0, health(at_s=20)),
    ]
    assert all(f.passed for f in evaluate(good, BY_PROCESS["api"]))
    never_down = [
        Observation("before", 0.0, health(at_s=0)),
        Observation("outage", 2.0, health(at_s=2)),
        *good[3:],
    ]
    failed = {f.check for f in evaluate(never_down, BY_PROCESS["api"]) if not f.passed}
    assert "health unreachable" in failed


def test_settle_kill_requires_frozen_alerts_and_ts_advance_on_recovery() -> None:
    frozen = [
        Observation("before", 0.0, health(at_s=0)),
        Observation("outage", 2.0, health(at_s=2, stuck={"settle"}, alerts=["ALR-FEED-STALE"])),
        Observation("outage", 20.0, health(at_s=20, stuck={"settle"}, alerts=["ALR-FEED-STALE"])),
        Observation("recovery", 6.0, health(at_s=26)),
    ]
    assert all(f.passed for f in evaluate(frozen, BY_PROCESS["settle"])), evaluate(
        frozen, BY_PROCESS["settle"]
    )
    thawed = [
        *frozen[:2],
        Observation("outage", 20.0, health(at_s=20, stuck={"settle"}, alerts=[])),
        frozen[3],
    ]
    failed = {f.check for f in evaluate(thawed, BY_PROCESS["settle"]) if not f.passed}
    assert any(c.startswith("health evaluation frozen") for c in failed)
