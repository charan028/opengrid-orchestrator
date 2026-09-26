"""Kill-each-process chaos runner (WORKBOARD Q3 / L4, TS-C).

For each process in `expectations.EXPECTATIONS`: snapshot health, kill, poll `GET /og/api/health` until the
expected outage state (or timeout), hold briefly to observe the steady state, restart, poll until recovered
(or timeout), evaluate against the expectation and write one Markdown report with PASS/FAIL per process and
the raw observation timeline.

    python tests-e2e/chaos/runner.py --backend dry-run --report chaos-dry.md      # no system needed
    python tests-e2e/chaos/runner.py --backend docker --report chaos.md           # dev stack
    python tests-e2e/chaos/runner.py --backend systemd --report chaos.md          # server, as root
    python tests-e2e/chaos/runner.py --backend systemd --only engine --report engine.md
    python tests-e2e/chaos/runner.py --backend systemd --include-infra --report full.md

`postgres`/`mosquitto` are skipped unless `--include-infra` is passed. Time is injected (`Clock`) so
`test_runner.py` runs the whole thing instantly.
"""

from __future__ import annotations

import argparse
import json
import logging
import sys
import time
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Protocol

import httpx

from backends import (
    ControllerError,
    DockerComposeController,
    DryRunController,
    HealthUnreachableError,
    ProcessController,
    SystemdController,
)
from expectations import (
    EXPECTATIONS,
    Expectation,
    Finding,
    Observation,
    evaluate,
    markdown_table,
)

log = logging.getLogger("chaos.runner")

DEFAULT_API_BASE = "http://127.0.0.1:8080"
HEALTH_PATH = "/og/api/health"


class Clock(Protocol):
    def now(self) -> float: ...

    def sleep(self, seconds: float) -> None: ...


@dataclass
class WallClock:
    def now(self) -> float:
        return time.time()

    def sleep(self, seconds: float) -> None:
        time.sleep(seconds)


@dataclass
class FakeClock:
    """Advances only when slept on, so a poll loop runs instantly in tests and the dry run."""

    t: float = 1_700_000_000.0

    def now(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.t += seconds


class HealthClient(Protocol):
    def get(self) -> dict[str, Any]:
        """Return the decoded health payload or raise `HealthUnreachableError`."""
        ...


@dataclass
class HttpHealthClient:
    client: httpx.Client
    url: str

    def get(self) -> dict[str, Any]:
        try:
            r = self.client.get(self.url)
            r.raise_for_status()
            payload = r.json()
        except (httpx.HTTPError, ValueError) as exc:
            raise HealthUnreachableError(str(exc)) from exc
        if not isinstance(payload, dict):
            raise HealthUnreachableError("health payload is not a JSON object")
        return payload


@dataclass
class CallableHealthClient:
    fn: Callable[[], dict[str, Any]]

    def get(self) -> dict[str, Any]:
        return self.fn()


@dataclass(frozen=True, slots=True)
class Settings:
    poll_interval_s: float = 2.0
    settle_hold_s: float = 10.0  # keep observing after the expected state so steady-state modes are seen
    outage_timeout_s: float = 60.0
    recovery_timeout_s: float = 120.0


@dataclass
class RowResult:
    expectation: Expectation
    timeline: list[Observation] = field(default_factory=list)
    findings: list[Finding] = field(default_factory=list)
    error: str | None = None

    @property
    def passed(self) -> bool:
        return self.error is None and all(f.passed for f in self.findings)


def _probe(health: HealthClient, phase: str, t_s: float) -> Observation:
    try:
        return Observation(phase, t_s, health=health.get())  # type: ignore[arg-type]
    except HealthUnreachableError as exc:
        return Observation(phase, t_s, error=str(exc))  # type: ignore[arg-type]


def _outage_reached(exp: Expectation, obs: Observation) -> bool:
    if exp.health_unreachable:
        return obs.error is not None
    if exp.health_frozen or exp.health_key is None:
        return False  # nothing to wait for: observe for the whole (short) window
    return (
        obs.health is not None
        and obs.health.get("processes", {}).get(exp.health_key, {}).get("status") == "down"
    )


def _recovered(exp: Expectation, obs: Observation) -> bool:
    if obs.health is None:
        return False
    processes = obs.health.get("processes", {})
    keys = [exp.health_key] if exp.health_key else list(processes)
    return all(processes.get(k, {}).get("status") == "ok" for k in keys)


def _poll(
    health: HealthClient,
    clock: Clock,
    *,
    phase: str,
    done: Callable[[Observation], bool],
    timeout_s: float,
    hold_s: float,
    interval_s: float,
    min_duration_s: float = 0.0,
) -> list[Observation]:
    """Poll until `done()` (then keep polling for `hold_s`) or until `timeout_s` since phase start, but never
    shorter than `min_duration_s` (slow effects such as hubs going offline need the outage to last)."""
    start = clock.now()
    reached_at: float | None = None
    out: list[Observation] = []
    while True:
        elapsed = clock.now() - start
        obs = _probe(health, phase, elapsed)
        out.append(obs)
        if reached_at is None and done(obs):
            reached_at = elapsed
        held = reached_at is not None and elapsed - reached_at >= hold_s
        if elapsed >= timeout_s or (held and elapsed >= min_duration_s):
            return out
        clock.sleep(interval_s)


def run_one(
    exp: Expectation,
    *,
    controller: ProcessController,
    health: HealthClient,
    clock: Clock,
    settings: Settings,
) -> RowResult:
    result = RowResult(exp)
    result.timeline.append(_probe(health, "before", 0.0))
    try:
        controller.kill(exp.unit)
        outage_timeout = max(
            min(settings.outage_timeout_s, (exp.down_within_s or 20.0) + settings.settle_hold_s + 10.0),
            exp.outage_observe_s + settings.settle_hold_s,
        )
        result.timeline += _poll(
            health,
            clock,
            phase="outage",
            done=lambda o: _outage_reached(exp, o),
            timeout_s=outage_timeout,
            hold_s=settings.settle_hold_s,
            interval_s=settings.poll_interval_s,
            min_duration_s=exp.outage_observe_s,
        )
        controller.start(exp.unit)
        result.timeline += _poll(
            health,
            clock,
            phase="recovery",
            done=lambda o: _recovered(exp, o),
            timeout_s=max(settings.recovery_timeout_s, exp.recovery_within_s + 10.0),
            hold_s=0.0,
            interval_s=settings.poll_interval_s,
        )
    except ControllerError as exc:
        result.error = str(exc)
        log.error("%s: controller failed: %s", exp.process, exc)
    result.findings = evaluate(result.timeline, exp)
    return result


def run_all(
    plan: Sequence[Expectation],
    *,
    controller: ProcessController,
    health: HealthClient,
    clock: Clock,
    settings: Settings,
) -> list[RowResult]:
    results: list[RowResult] = []
    for exp in plan:
        log.info("=== %s (%s)", exp.process, exp.unit)
        results.append(run_one(exp, controller=controller, health=health, clock=clock, settings=settings))
        log.info("%s: %s", exp.process, "PASS" if results[-1].passed else "FAIL")
    return results


def select_plan(*, only: str | None, include_infra: bool) -> list[Expectation]:
    plan = [e for e in EXPECTATIONS if include_infra or not e.infra]
    if only is not None:
        plan = [e for e in plan if e.process == only]
        if not plan:
            raise SystemExit(f"unknown process {only!r}; choose from {[e.process for e in EXPECTATIONS]}")
    return plan


# --- report ----------------------------------------------------------------------------------------------


def _obs_line(o: Observation) -> str:
    if o.health is None:
        return f"| {o.phase} | {o.t_s:6.1f} | unreachable | - | - | {o.error} |"
    h = o.health
    down = sorted(
        k for k, v in h.get("processes", {}).items() if isinstance(v, dict) and v.get("status") != "ok"
    )
    modes = h.get("degraded_modes")
    alerts = sorted({a.get("rule", "?") for a in h.get("alerts", []) if isinstance(a, dict)})
    return f"| {o.phase} | {o.t_s:6.1f} | {down or 'all ok'} | {modes if modes is not None else 'n/a'} | {alerts or '-'} | |"


def render_report(results: Sequence[RowResult], *, meta: dict[str, Any]) -> str:
    verdict = "PASS" if results and all(r.passed for r in results) else "FAIL"
    lines = [
        f"# Chaos run -- {verdict}",
        "",
        f"Generated {datetime.now(UTC).isoformat(timespec='seconds')}; "
        + ", ".join(f"{k}={v}" for k, v in meta.items()),
        "",
        "## Summary",
        "",
        "| process | unit | result | failed checks |",
        "|---|---|---|---|",
    ]
    for r in results:
        failed = [f.check for f in r.findings if not f.passed]
        lines.append(
            f"| {r.expectation.process} | {r.expectation.unit} | {'PASS' if r.passed else 'FAIL'} | "
            f"{r.error or ('; '.join(failed) if failed else '-')} |"
        )
    lines += [
        "",
        "## Expectations (from `expectations.py`)",
        "",
        markdown_table([r.expectation for r in results]),
        "",
    ]
    for r in results:
        lines += [
            f"## {r.expectation.process} -- {'PASS' if r.passed else 'FAIL'}",
            "",
            f"Source: {r.expectation.source}",
            "",
            "| check | result | detail |",
            "|---|---|---|",
            *(f"| {f.check} | {'PASS' if f.passed else 'FAIL'} | {f.detail} |" for f in r.findings),
            "",
            "<details><summary>observation timeline</summary>",
            "",
            "| phase | t (s) | processes not ok | degraded_modes | open alert rules | error |",
            "|---|---|---|---|---|---|",
            *(_obs_line(o) for o in r.timeline),
            "",
            "</details>",
            "",
        ]
    return "\n".join(lines)


# --- CLI --------------------------------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--backend", choices=("systemd", "docker", "dry-run"), required=True)
    p.add_argument("--api-base", default=DEFAULT_API_BASE)
    p.add_argument("--only", metavar="PROCESS", help="run a single row (e.g. engine)")
    p.add_argument("--report", type=Path, required=True)
    p.add_argument("--include-infra", action="store_true", help="also kill postgres and mosquitto")
    defaults = Settings()
    p.add_argument("--compose-file", default=DockerComposeController().compose_file)
    p.add_argument("--poll-interval-s", type=float, default=defaults.poll_interval_s)
    p.add_argument("--settle-hold-s", type=float, default=defaults.settle_hold_s)
    p.add_argument("--timeout-s", type=float, default=5.0, help="per-request timeout for the health probe")
    return p


def main(argv: list[str] | None = None) -> int:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s", stream=sys.stderr)
    args = build_parser().parse_args(argv)
    plan = select_plan(only=args.only, include_infra=args.include_infra)
    settings = Settings(poll_interval_s=args.poll_interval_s, settle_hold_s=args.settle_hold_s)
    meta = {"backend": args.backend, "api_base": args.api_base, "rows": len(plan)}

    if args.backend == "dry-run":
        clock: Clock = FakeClock()
        dry = DryRunController(clock=clock.now)
        results = run_all(
            plan,
            controller=dry,
            health=CallableHealthClient(dry.health_payload),
            clock=clock,
            settings=settings,
        )
        meta["controller_calls"] = json.dumps(dry.calls)
    else:
        controller: ProcessController = (
            SystemdController()
            if args.backend == "systemd"
            else DockerComposeController(compose_file=args.compose_file)
        )
        with httpx.Client(timeout=args.timeout_s) as client:
            health = HttpHealthClient(client, f"{args.api_base.rstrip('/')}{HEALTH_PATH}")
            results = run_all(
                plan, controller=controller, health=health, clock=WallClock(), settings=settings
            )

    args.report.parent.mkdir(parents=True, exist_ok=True)
    args.report.write_text(render_report(results, meta=meta), encoding="utf-8")
    verdict = "PASS" if all(r.passed for r in results) else "FAIL"
    sys.stdout.write(
        f"{verdict} -- {sum(r.passed for r in results)}/{len(results)} rows; report {args.report}\n"
    )
    return 0 if verdict == "PASS" else 1


if __name__ == "__main__":
    sys.exit(main())
