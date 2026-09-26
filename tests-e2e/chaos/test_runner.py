"""End-to-end runner tests with the DryRunController and a fake clock: PASS on a correct scripted system,
FAIL when the scripted system violates an expectation."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from backends import DryRunController
from expectations import BY_PROCESS, EXPECTATIONS
from runner import CallableHealthClient, FakeClock, Settings, main, render_report, run_all, select_plan

FAST = Settings(poll_interval_s=2.0, settle_hold_s=6.0)


def _run(mutate=None, plan=None):
    clock = FakeClock()
    dry = DryRunController(clock=clock.now, mutate=mutate)
    results = run_all(
        plan or list(EXPECTATIONS),
        controller=dry,
        health=CallableHealthClient(dry.health_payload),
        clock=clock,
        settings=FAST,
    )
    return dry, results, render_report(results, meta={"backend": "dry-run"})


def test_correct_system_passes_every_row_including_infra() -> None:
    dry, results, report = _run()
    assert all(r.passed for r in results), [
        (r.expectation.process, [f for f in r.findings if not f.passed]) for r in results
    ]
    assert report.startswith("# Chaos run -- PASS")
    assert report.count("| PASS |") >= len(EXPECTATIONS)
    kills = [u for op, u in dry.calls if op == "kill"]
    assert kills == [e.unit for e in EXPECTATIONS]
    assert dry.calls.count(("start", "og-engine")) == 1


def test_safestop_dying_with_engine_fails_the_engine_row() -> None:
    def safestop_dies_too(payload: dict[str, Any], killed: set[str]) -> dict[str, Any]:
        if "engine" in killed:
            payload["processes"]["safestop"]["status"] = "down"
        return payload

    _, results, report = _run(safestop_dies_too, plan=[BY_PROCESS["engine"], BY_PROCESS["guardian"]])
    by = {r.expectation.process: r for r in results}
    assert not by["engine"].passed and by["guardian"].passed
    assert report.startswith("# Chaos run -- FAIL")
    assert "| engine | og-engine | FAIL |" in report and "safestop" in report


def test_missing_degraded_mode_fails_guardian_row() -> None:
    def no_hold(payload: dict[str, Any], killed: set[str]) -> dict[str, Any]:
        payload["degraded_modes"] = []
        return payload

    _, results, _ = _run(no_hold, plan=[BY_PROCESS["guardian"]])
    failed = [f.check for f in results[0].findings if not f.passed]
    assert failed == ["degraded_modes contains ['HOLD']"]


def test_select_plan_filters_infra_and_only() -> None:
    assert [e.process for e in select_plan(only=None, include_infra=False)] == list(BY_PROCESS)[:7]
    assert [e.process for e in select_plan(only="postgres", include_infra=True)] == ["postgres"]
    with pytest.raises(SystemExit):
        select_plan(only="postgres", include_infra=False)  # infra rows need --include-infra


def test_cli_dry_run_writes_report(tmp_path: Path) -> None:
    report = tmp_path / "chaos.md"
    assert main(["--backend", "dry-run", "--report", str(report), "--include-infra"]) == 0
    text = report.read_text(encoding="utf-8")
    assert text.startswith("# Chaos run -- PASS")
    assert "| postgres | postgresql | PASS |" in text
    assert "observation timeline" in text
