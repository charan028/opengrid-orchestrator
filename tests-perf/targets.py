"""tests-perf/targets.py -- where the isolated perf stack runs, behind one small interface.

Two targets, chosen by TARGET in <run>/ports.env (written by perfenv.py):

- `compose`: the repo's Docker dev stack (dev/docker-compose.yml + the orchestrator profile) as its own compose
  project (`ogperf`), with tests-perf/compose/docker-compose.perf.yml layered on top. Works on any host with
  Docker (Linux, or Windows/macOS with Docker Desktop). Process figures come from each container's cgroup, disk
  figures from the Docker VM's /proc/diskstats (read inside the postgres container), metrics endpoints through
  `docker compose exec` (og-engine binds its metrics to loopback inside the container).
- `base`: transient systemd units `ogperf-*` started by stack.sh on a Linux host next to production (the
  2026-09-26 base-server setup). Only used with the production guardrail (watchdog.sh) running.

The harness (sampler.py, stress.py, campaign.py) only talks to a Target; everything else is plain TCP to the
published ports.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Any

import httpx

#: harness process name -> compose service name
COMPOSE_SERVICES = {
    "mosquitto": "mosquitto",
    "sim-market": "sim-market",
    "sim-fleet": "sim-fleet",
    "sim-scada": "sim-scada",
    "safestop": "og-safestop",
    "guardian": "og-guardian",
    "engine": "og-engine",
    "feeds": "og-feeds",
    "settle": "og-settle",
    "api": "og-api",
    "postgres": "postgres",
}
BASE_DISKS = {"dm-7": "pgstandby", "dm-6": "pgdata", "dm-0": "root", "sda": "sda"}
SCRAPE_PY = (
    "import sys, urllib.request as u; sys.stdout.write(u.urlopen(sys.argv[1], timeout=5).read().decode())"
)


def read_env(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, _, v = line.partition("=")
                values[k.strip()] = v.strip()
    return values


def _run(argv: list[str], timeout: float = 30.0) -> str:
    try:
        return subprocess.run(argv, capture_output=True, text=True, timeout=timeout, check=False).stdout
    except (OSError, subprocess.TimeoutExpired):
        return ""


def parse_diskstats(text: str, names: dict[str, str] | None = None) -> dict[str, dict[str, float]]:
    """/proc/diskstats -> {name: counters}. With `names`, only those devices (renamed); without, every
    non-virtual whole device."""
    out: dict[str, dict[str, float]] = {}
    for line in text.splitlines():
        f = line.split()
        if len(f) < 14:
            continue
        dev = f[2]
        if names is not None:
            if dev not in names:
                continue
            key = names[dev]
        else:
            if dev.startswith(("loop", "ram", "zram", "dm-", "nbd")) or (
                dev[-1].isdigit() and not dev.startswith("nvme")
            ):
                continue
            key = dev
        out[key] = {
            "reads": float(f[3]),
            "writes": float(f[7]),
            "sectors_w": float(f[9]),
            "write_ms": float(f[10]),
            "io_ms": float(f[12]),
        }
    return out


def parse_host(stat: str, meminfo: str, loadavg: str) -> dict[str, float]:
    vals = [float(x) for x in stat.splitlines()[0].split()[1:]]
    mem: dict[str, float] = {}
    for line in meminfo.splitlines():
        k, _, v = line.partition(":")
        if k in ("MemTotal", "MemAvailable"):
            mem[k] = float(v.split()[0]) / 1024.0
    return {
        "cpu_total_ticks": sum(vals),
        "cpu_idle_ticks": vals[3] + vals[4],
        "load1": float(loadavg.split()[0]) if loadavg else 0.0,
        "mem_total_mb": mem.get("MemTotal", 0.0),
        "mem_available_mb": mem.get("MemAvailable", 0.0),
    }


class Target:
    name = "abstract"

    def __init__(self, run: Path, ports: dict[str, str], repo: Path) -> None:
        self.run = run
        self.ports = ports
        self.repo = repo

    def scrape(self, proc: str, port: str) -> str:
        raise NotImplementedError

    def procs(self) -> dict[str, dict[str, Any] | None]:
        raise NotImplementedError

    def disks(self) -> dict[str, dict[str, float]]:
        raise NotImplementedError

    def host(self) -> dict[str, float]:
        raise NotImplementedError

    def broker(self, action: str) -> None:
        """`stop` or `start` the perf broker only."""
        raise NotImplementedError

    def restarts(self, proc: str) -> int:
        raise NotImplementedError


class BaseTarget(Target):
    """systemd transient units `ogperf-<name>` (stack.sh), read from /proc on the same Linux host."""

    name = "base"

    def scrape(self, proc: str, port: str) -> str:
        return httpx.get(f"http://127.0.0.1:{port}/metrics", timeout=5.0).text

    def _main_pid(self, unit: str) -> int:
        out = _run(
            ["/usr/bin/systemctl", "show", "-p", "MainPID", "--value", f"ogperf-{unit}.service"], 5
        ).strip()
        return int(out) if out.isdigit() else 0

    def procs(self) -> dict[str, dict[str, Any] | None]:
        clk = os.sysconf("SC_CLK_TCK")
        out: dict[str, dict[str, Any] | None] = {}
        for p in COMPOSE_SERVICES:
            if p == "postgres":
                continue
            pid = self._main_pid(p)
            out[p] = None
            if pid <= 0:
                continue
            try:
                stat = Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()
                rss = next(
                    (
                        float(x.split()[1])
                        for x in Path(f"/proc/{pid}/status").read_text().splitlines()
                        if x.startswith("VmRSS:")
                    ),
                    0.0,
                )
                out[p] = {"pid": pid, "cpu_s": (int(stat[11]) + int(stat[12])) / clk, "rss_mb": rss / 1024.0}
            except (OSError, IndexError, ValueError):
                out[p] = None
        return out

    def disks(self) -> dict[str, dict[str, float]]:
        return parse_diskstats(Path("/proc/diskstats").read_text(), BASE_DISKS)

    def host(self) -> dict[str, float]:
        return parse_host(
            Path("/proc/stat").read_text(),
            Path("/proc/meminfo").read_text(),
            Path("/proc/loadavg").read_text(),
        )

    def broker(self, action: str) -> None:
        cmd = {"stop": "broker-down", "start": "broker-up"}[action]
        subprocess.run(
            ["/bin/bash", str(self.repo / "tests-perf" / "stack.sh"), "--run", str(self.run), cmd], check=True
        )

    def restarts(self, proc: str) -> int:
        out = _run(
            ["/usr/bin/systemctl", "show", "-p", "NRestarts", "--value", f"ogperf-{proc}.service"], 5
        ).strip()
        return int(out) if out.isdigit() else -1


class ComposeTarget(Target):
    """The Docker dev stack as compose project `ogperf` (dev/docker-compose.yml + the perf override)."""

    name = "compose"

    def __init__(self, run: Path, ports: dict[str, str], repo: Path) -> None:
        super().__init__(run, ports, repo)
        self.docker = os.environ.get("DOCKER", "docker")
        self.compose = [
            self.docker,
            "compose",
            "-p",
            ports.get("COMPOSE_PROJECT", "ogperf"),
            "-f",
            str(repo / "dev" / "docker-compose.yml"),
            "-f",
            str(repo / "tests-perf" / "compose" / "docker-compose.perf.yml"),
            "--profile",
            "orchestrator",
        ]
        self._ids: dict[str, str] = {}
        self._disk: str | None = None

    def cid(self, proc: str) -> str:
        cid = _run([*self.compose, "ps", "-q", COMPOSE_SERVICES[proc]], 20).strip().splitlines()
        self._ids[proc] = cid[0] if cid else ""
        return self._ids[proc]

    def exec(self, proc: str, argv: list[str], timeout: float = 20.0) -> str:
        cid = self._ids.get(proc) or self.cid(proc)
        if not cid:
            return ""
        out = _run([self.docker, "exec", cid, *argv], timeout)
        if not out:  # container replaced (restart/recreate): look the id up again once
            cid = self.cid(proc)
            out = _run([self.docker, "exec", cid, *argv], timeout) if cid else ""
        return out

    def scrape(self, proc: str, port: str) -> str:
        return self.exec(proc, ["python", "-c", SCRAPE_PY, f"http://127.0.0.1:{port}/metrics"])

    def procs(self) -> dict[str, dict[str, Any] | None]:
        out: dict[str, dict[str, Any] | None] = {}
        for p in COMPOSE_SERVICES:
            text = self.exec(p, ["cat", "/sys/fs/cgroup/cpu.stat", "/sys/fs/cgroup/memory.stat"], 10)
            vals: dict[str, float] = {}
            for line in text.splitlines():
                k, _, v = line.partition(" ")
                if k in ("usage_usec", "anon") and v.strip().isdigit():
                    vals[k] = float(v)
            out[p] = (
                {
                    "pid": self._ids.get(p, ""),
                    "cpu_s": vals["usage_usec"] / 1e6,
                    "rss_mb": vals.get("anon", 0.0) / 1048576.0,
                }
                if "usage_usec" in vals
                else None
            )
        return out

    def disks(self) -> dict[str, dict[str, float]]:
        all_devs = parse_diskstats(self.exec("postgres", ["cat", "/proc/diskstats"], 10))
        if self._disk is None and all_devs:
            self._disk = max(all_devs, key=lambda d: all_devs[d]["sectors_w"])  # the VM's busiest disk
        return {"db_disk": all_devs[self._disk]} if self._disk in all_devs else {}

    def host(self) -> dict[str, float]:
        text = self.exec(
            "postgres",
            ["sh", "-c", "cat /proc/stat; echo ---; cat /proc/meminfo; echo ---; cat /proc/loadavg"],
        )
        parts = text.split("---\n")
        return parse_host(parts[0], parts[1], parts[2]) if len(parts) == 3 else {}

    def broker(self, action: str) -> None:
        subprocess.run([*self.compose, action, "mosquitto"], check=True, capture_output=True)

    def restarts(self, proc: str) -> int:
        cid = self._ids.get(proc) or self.cid(proc)
        out = _run([self.docker, "inspect", "-f", "{{.RestartCount}}", cid], 10).strip() if cid else ""
        return int(out) if out.isdigit() else -1


def load_target(run: Path, repo: Path) -> Target:
    ports = read_env(run / "ports.env")
    kind = ports.get("TARGET", "base")
    return ComposeTarget(run, ports, repo) if kind == "compose" else BaseTarget(run, ports, repo)
