"""Process controllers for the chaos runner: systemd (server), docker compose (dev stack), dry-run (tests).

- `SystemdController`: `systemctl kill|start|is-active <unit>` -- what the lead runs as root on the server
  (`deploy/README.md` "Start / stop / status"; units in `deploy/systemd/og-*.service`).
- `DockerComposeController`: `docker compose -f <file> kill|start|ps <service>`. The dev stack (WORKBOARD D0,
  `dev/docker-compose.yml`) does not exist yet, so the command templates are configurable.
- `DryRunController`: records every call and serves a *scripted* health payload that follows the expected
  behaviour table (`expectations.py`) so the runner and its report can be exercised with no system at all.

Every subprocess call has a timeout (BUILD.md S5a).
"""

from __future__ import annotations

import logging
import subprocess
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from expectations import (
    ALL_PROCESSES,
    BY_PROCESS,
    HEARTBEAT_DOWN_AFTER_S,
    HUB_OFFLINE_S,
)

log = logging.getLogger("chaos.backends")


class ControllerError(RuntimeError):
    """A kill/start command failed or timed out."""


class ProcessController(Protocol):
    def kill(self, unit: str) -> None: ...

    def start(self, unit: str) -> None: ...

    def is_running(self, unit: str) -> bool: ...


def _run(cmd: list[str], *, timeout_s: float, check: bool) -> subprocess.CompletedProcess[str]:
    log.info("exec %s", " ".join(cmd))
    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise ControllerError(f"{cmd[0]} failed: {exc}") from exc
    if check and result.returncode != 0:
        raise ControllerError(f"{' '.join(cmd)} exited {result.returncode}: {result.stderr.strip()}")
    return result


@dataclass(frozen=True, slots=True)
class SystemdController:
    """`systemctl kill` sends SIGKILL to the unit's processes without stopping the unit, so `Restart=always`
    would bring it back after `RestartSec=2` on its own; the runner still issues `start` explicitly so the
    recovery phase has a defined t=0."""

    timeout_s: float = 20.0
    signal: str = "SIGKILL"

    def kill(self, unit: str) -> None:
        _run(["systemctl", "kill", f"--signal={self.signal}", unit], timeout_s=self.timeout_s, check=True)

    def start(self, unit: str) -> None:
        _run(["systemctl", "start", unit], timeout_s=self.timeout_s, check=True)

    def is_running(self, unit: str) -> bool:
        r = _run(["systemctl", "is-active", "--quiet", unit], timeout_s=self.timeout_s, check=False)
        return r.returncode == 0


@dataclass(frozen=True, slots=True)
class DockerComposeController:
    compose_file: str = "dev/docker-compose.yml"
    timeout_s: float = 60.0
    # {file} and {service} are substituted; override when the dev stack names things differently.
    kill_cmd: str = "docker compose -f {file} kill {service}"
    start_cmd: str = "docker compose -f {file} start {service}"
    ps_cmd: str = "docker compose -f {file} ps --status running --services"

    def _cmd(self, template: str, service: str) -> list[str]:
        return template.format(file=self.compose_file, service=service).split()

    def kill(self, unit: str) -> None:
        _run(self._cmd(self.kill_cmd, unit), timeout_s=self.timeout_s, check=True)

    def start(self, unit: str) -> None:
        _run(self._cmd(self.start_cmd, unit), timeout_s=self.timeout_s, check=True)

    def is_running(self, unit: str) -> bool:
        r = _run(self._cmd(self.ps_cmd, unit), timeout_s=self.timeout_s, check=True)
        return unit in r.stdout.split()


class HealthUnreachableError(RuntimeError):
    """`GET /og/api/health` could not be served (connection refused, timeout, 5xx)."""


HealthMutator = Callable[[dict[str, Any], set[str]], dict[str, Any]]


@dataclass
class DryRunController:
    """Records kill/start calls and synthesises the health read model a *correct* system would show.

    `clock` supplies wall-clock seconds; `health_payload()` derives each process's status from how long it
    has been dead (down after `HEARTBEAT_DOWN_AFTER_S`), the degraded modes from `health/rules.py`, and
    the freeze/unreachable behaviours for settle/api/postgres. `mutate` lets tests corrupt the payload
    (e.g. mark safestop down too) to prove the evaluator fails a wrong system.
    """

    clock: Callable[[], float]
    calls: list[tuple[str, str]] = field(default_factory=list)
    killed_at: dict[str, float] = field(default_factory=dict)  # process -> clock time of the kill
    mutate: HealthMutator | None = None
    _unit_to_process: dict[str, str] = field(
        default_factory=lambda: {e.unit: e.process for e in BY_PROCESS.values()}
    )
    _frozen: dict[str, Any] | None = None
    _last_beat: dict[str, float] = field(default_factory=dict)

    def kill(self, unit: str) -> None:
        self.calls.append(("kill", unit))
        self.killed_at[self._unit_to_process.get(unit, unit)] = self.clock()

    def start(self, unit: str) -> None:
        self.calls.append(("start", unit))
        self.killed_at.pop(self._unit_to_process.get(unit, unit), None)
        if self._unit_to_process.get(unit) == "settle":
            self._frozen = None

    def is_running(self, unit: str) -> bool:
        return self._unit_to_process.get(unit, unit) not in self.killed_at

    def _dead_for(self, process: str) -> float | None:
        return None if process not in self.killed_at else self.clock() - self.killed_at[process]

    def health_payload(self) -> dict[str, Any]:
        now = self.clock()
        if "api" in self.killed_at or "postgres" in self.killed_at:
            raise HealthUnreachableError("connection refused")
        down: set[str] = set()
        processes: dict[str, Any] = {}
        for p in ALL_PROCESSES:
            dead = self._dead_for(p)
            if dead is None:
                self._last_beat[p] = now
            status = "down" if dead is not None and dead >= HEARTBEAT_DOWN_AFTER_S else "ok"
            if status == "down":
                down.add(p)
            ts = datetime.fromtimestamp(self._last_beat.get(p, now), UTC).isoformat()
            processes[p] = {"pid": 1000 + len(processes), "ts": ts, "status": status}
        modes = sorted(
            m for m, p in (("HOLD_LOCAL_AUTONOMY", "engine"), ("HOLD", "guardian")) if p in down
        )  # health/rules.py derive_degraded_modes
        alerts = [
            {"rule": "ALR-PROCESS-DOWN", "severity": "critical", "summary": f"Process {p} heartbeat missing"}
            for p in sorted(down)
        ]
        mq_dead = self._dead_for("mosquitto")
        sim_dead = self._dead_for("sim")
        hubs_offline = (mq_dead is not None and mq_dead >= HUB_OFFLINE_S) or (
            sim_dead is not None and sim_dead >= HUB_OFFLINE_S
        )
        if hubs_offline:
            alerts.append(
                {
                    "rule": "ALR-HUB-OFFLINE-RATIO",
                    "severity": "critical",
                    "summary": "Zone LZ_NORTH hub offline ratio 100%",
                }
            )
        evaluated = {
            "alerts": alerts,
            "hub_health_counts": {"offline": 2000} if hubs_offline else {"online": 2000},
            "degraded_modes": modes,
        }
        if "settle" in self.killed_at:
            self._frozen = self._frozen or evaluated
            evaluated = self._frozen
        payload: dict[str, Any] = {
            "status": "ok",
            "as_of": datetime.fromtimestamp(now, UTC).isoformat(),
            "processes": processes,
            "open_alert_count": len(evaluated["alerts"]),
            "reserve_breaches": 0,
            "double_sold_kwh": 0,
            "commitment_switches": 0,
            **evaluated,
        }
        return self.mutate(payload, set(self.killed_at)) if self.mutate else payload
