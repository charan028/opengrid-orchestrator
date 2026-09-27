#!/usr/bin/env python3
"""tests-perf/campaign.py -- the full scalability + stress campaign, on either target.

    python tests-perf/campaign.py --target compose                      # Docker dev stack (the default)
    python tests-perf/campaign.py --target compose --steps "7500" --soak-min 0 --no-stress
    python tests-perf/campaign.py --target base --guard on               # beside production (root, Linux)

For every fleet size (home hubs; hubs = homes + 9), each size is measured in two regimes, as production runs:
1. Setup: perfenv.py -> fresh database -> stack up -> `--history` (14 days of ERCOT prices/load, so the forecast
   is FIRM) -> og-feeds restart (forecast recompute).
2. IDLE: warm-up, then the measured window (stage `step-<homes>`). This is production between delivery windows.
3. DELIVERING: dispatch.py commits obligations until about `--deliver-frac` of the home banks carry grants for a
   window, then the measured window (stage `deliver-<homes>`). Skip it with `--no-deliver`.

At the largest size the soak follows in the delivering regime (stage `soak-<homes>`, soak-min minus deliver-min),
then the stress scenarios (stages `stress-<name>`). sampler.py runs as a child process for the whole campaign and
tags every sample with <run>/STAGE.

Guardrail (`--guard auto|on|off`, auto = on for `base`, off for `compose`):
- base: tests-perf/watchdog.sh as unit ogperf-watchdog watches PRODUCTION (cycle p99, pgdata util, fresh hubs,
  memory) and aborts the run;
- compose: a local guard in this process aborts when the Docker VM's MemAvailable drops below --min-mem-mb.
On an abort the campaign waits until the guard has been clear for 2 minutes, then repeats that size once; a
second abort ends the campaign. Every abort is in <run>/aborts.jsonl.
"""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

import psycopg

import dispatch
import perfenv
from sampler import dsn_from
from targets import ComposeTarget, Target, load_target, read_env

HERE = Path(__file__).resolve().parent
REPO = HERE.parent
STRESS_DEFAULT = "price bulk alerts burst outage dbslow safestop"
#: og.feed_obs history exported from a dev database that ran orchestrator/tools/ercot_backfill.py (HANDOFF 1).
HISTORY_DEFAULT = HERE / ".cache" / "ercot_history.csv"
HISTORY_COPY = (
    "copy hist (source, product, series, ts, value, unit, quality, recorded_at) from stdin with (format csv)"
)
#: The stress scenarios take about 40 min; the delivery window must still be open while they run.
STRESS_ALLOWANCE_MIN = 45.0


class AbortedError(Exception):
    pass


class Campaign:
    def __init__(self, args: argparse.Namespace) -> None:
        self.a = args
        self.run: Path = args.run
        self.guard = args.guard == "on" or (args.guard == "auto" and args.target == "base")
        self.sampler: subprocess.Popen[bytes] | None = None
        self.target: Target | None = None
        for sub in ("data", "logs"):
            (self.run / sub).mkdir(parents=True, exist_ok=True)

    # --- bookkeeping ------------------------------------------------------------------------------------
    def log(self, msg: str) -> None:
        line = f"{datetime.now(UTC).isoformat(timespec='seconds')} {msg}"
        print(line, flush=True)
        with (self.run / "logs" / "campaign.log").open("a", encoding="utf-8") as fh:
            fh.write(line + "\n")

    def stage(self, name: str) -> None:
        (self.run / "STAGE").write_text(name)
        with (self.run / "data" / "stages.tsv").open("a", encoding="utf-8") as fh:
            fh.write(f"{datetime.now(UTC).isoformat()}\t{name}\n")
        self.log(f"STAGE {name}")

    def aborted(self) -> bool:
        return (self.run / "ABORT").exists()

    def local_guard(self) -> None:
        """compose + --guard on: the Docker VM's MemAvailable floor."""
        if not (self.guard and self.a.target == "compose" and self.target):
            return
        mem = self.target.host().get("mem_available_mb")
        breach = mem is not None and mem < self.a.min_mem_mb
        rec = {
            "ts": datetime.now(UTC).isoformat(),
            "mem_available_mb": mem,
            "breach": "mem" if breach else "",
        }
        with (self.run / "data" / "guard.jsonl").open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(rec) + "\n")
        if breach and not self.aborted():
            stage = (self.run / "STAGE").read_text()
            with (self.run / "aborts.jsonl").open("a", encoding="utf-8") as fh:
                fh.write(
                    json.dumps(
                        {**rec, "stage": stage, "reasons": f"mem_available_{mem}MB<{self.a.min_mem_mb}"}
                    )
                    + "\n"
                )
            (self.run / "ABORT").write_text(json.dumps(rec))

    def sleep(self, seconds: float) -> None:
        end = time.time() + seconds
        while time.time() < end:
            self.local_guard()
            if self.aborted():
                raise AbortedError
            time.sleep(min(15.0, max(0.0, end - time.time())))

    def wait_guard_clear(self, timeout_s: float = 1800) -> bool:
        guard = self.run / "data" / "guard.jsonl"
        deadline = time.time() + timeout_s
        while time.time() < deadline:
            self.local_guard()
            lines = guard.read_text().splitlines()[-8:] if guard.exists() else []
            if not self.guard or (
                len(lines) == 8 and all('"breach":""' in x.replace(" ", "") for x in lines)
            ):
                return True
            time.sleep(15)
        return False

    # --- stack control ---------------------------------------------------------------------------------
    def sh(self, argv: list[str], log_name: str | None = None, check: bool = True) -> int:
        out = (self.run / "logs" / log_name).open("a") if log_name else None
        try:
            rc = subprocess.run(
                argv, stdout=out, stderr=subprocess.STDOUT if out else None, check=False
            ).returncode
        finally:
            if out:
                out.close()
        if check and rc != 0:
            raise RuntimeError(f"{' '.join(argv[:6])} ... exited {rc}")
        return rc

    def stack_sh(self, cmd: str) -> None:
        self.sh(["bash", str(HERE / "stack.sh"), "--run", str(self.run), cmd], "stack.log")

    def setup(self, homes: int) -> None:
        if self.a.target == "compose":
            perfenv.write_compose(REPO, self.run, homes)
            self.target = load_target(self.run, REPO)
            if not isinstance(self.target, ComposeTarget):
                raise RuntimeError("ports.env does not describe a compose target")
            compose = self.target.compose
            self.sh([*compose, "down", "-v", "--remove-orphans"], "compose.log", check=False)
            self.sh([*compose, "up", "-d", "--build"], "compose.log")
        else:
            perfenv.write_base(REPO, self.run, homes)
            self.target = load_target(self.run, REPO)
            for cmd in ("keys", "db", "up"):
                self.stack_sh(cmd)
        self.wait_seeded()
        if self.a.target == "compose" and self.a.history:
            self.load_history()
            self.refresh_forecast()

    def _dsn(self, app: str) -> str:
        return dsn_from(read_env(self.run / "ports.env"), read_env(self.run / "etc" / "secrets.env"), app)

    def load_history(self) -> None:
        """--history: og.feed_obs rows into the fresh database, so the forecast is FIRM and the selector can
        commit (a fresh database is NOT_FOR_FIRM everywhere). Real ERCOT data copied from a dev database; no live
        ERCOT call is made here."""
        path: Path = self.a.history
        with psycopg.connect(self._dsn("ogperf-history")) as conn, conn.cursor() as cur:
            cur.execute("create temp table hist (like og.feed_obs including defaults) on commit drop")
            with cur.copy(HISTORY_COPY) as copy, path.open("rb") as fh:
                while chunk := fh.read(1 << 16):
                    copy.write(chunk)
            cur.execute("insert into og.feed_obs select * from hist on conflict do nothing")
            loaded = cur.rowcount
            conn.commit()
        self.log(f"history: {loaded} og.feed_obs rows from {path.name}")

    def refresh_forecast(self, timeout_s: float = 300) -> None:
        """Restart og-feeds (it recomputes the forecast at start) and wait for FIRM price rows."""
        if not isinstance(self.target, ComposeTarget):
            return
        since = datetime.now(UTC)
        self.sh([*self.target.compose, "restart", "og-feeds"], "compose.log")
        deadline = time.time() + timeout_s
        firm = total = 0
        while time.time() < deadline:
            with psycopg.connect(self._dsn("ogperf-campaign"), autocommit=True) as conn:
                row = conn.execute(
                    "select count(*) filter (where firm_fitness <> 'NOT_FOR_FIRM'), count(*) from og.forecast "
                    "where kind = 'price' and computed_at > %s",
                    (since,),
                ).fetchone()
            firm, total = (int(row[0]), int(row[1])) if row else (0, 0)
            if firm:
                break
            time.sleep(10)
        self.log(f"forecast: {firm}/{total} price rows firm after the history load")

    def start_delivery(self, homes: int, last: bool) -> datetime:
        """DELIVERING regime: commit obligations for a window that covers the measured window (and, at the
        largest size, the soak and the stress scenarios). Returns the window start."""
        length = 2.0 + self.a.deliver_min + 10.0
        if last:
            length += max(0.0, self.a.soak_min - self.a.deliver_min)
            length += STRESS_ALLOWANCE_MIN if self.a.stress else 0.0
        window = dispatch.window_for(datetime.now(UTC), lead_min=4.0, length_min=length)
        ports, secrets = read_env(self.run / "ports.env"), read_env(self.run / "etc" / "secrets.env")
        api = dispatch.Api(f"http://127.0.0.1:{ports['API_PORT']}/og/api", secrets["OG_API_PROXY_SECRET"])
        result = dispatch.drive(
            REPO,
            api,
            self._dsn("ogperf-dispatch"),
            window,
            self.a.deliver_frac,
            self.log,
            kw_per_offer=self.a.deliver_kw,
        )
        (self.run / "data" / f"dispatch-{homes}.json").write_text(json.dumps(result, indent=2))
        self.log(
            f"dispatch: window {window[0]:%H:%M}-{window[1]:%H:%M} UTC, reserved banks {result['reserved_banks']}/"
            f"{result['home_banks']} (target {result['target_banks']}), offers {result['offers']}"
        )
        return window[0]

    def sleep_until(self, when: datetime) -> None:
        self.sleep(max(0.0, (when - datetime.now(UTC)).total_seconds()))

    def wait_seeded(self, timeout_s: float = 900) -> None:
        ports, secrets = read_env(self.run / "ports.env"), read_env(self.run / "etc" / "secrets.env")
        expected = int(ports["EXPECTED_HUBS"])
        deadline = time.time() + timeout_s
        n = -1
        while time.time() < deadline:
            try:
                with psycopg.connect(dsn_from(ports, secrets, "ogperf-campaign"), autocommit=True) as conn:
                    row = conn.execute("select count(*) from og.hub").fetchone()
                    n = int(row[0]) if row else -1
            except psycopg.Error:
                n = -1
            if n == expected:
                self.log(f"seeded: og.hub = {n}")
                return
            time.sleep(10)
        raise RuntimeError(f"seed did not reach {expected} hubs within {timeout_s:.0f} s (last {n})")

    def teardown(self) -> None:
        if self.a.target == "compose" and isinstance(self.target, ComposeTarget):
            self.sh([*self.target.compose, "down", "-v", "--remove-orphans"], "compose.log", check=False)
        elif self.a.target == "base":
            self.stack_sh("down")

    def start_sampler(self) -> None:
        if self.sampler is None or self.sampler.poll() is not None:
            log = (self.run / "logs" / "sampler.log").open("a")
            self.sampler = subprocess.Popen(
                [sys.executable, str(HERE / "sampler.py"), "--run", str(self.run), "--repo", str(REPO)],
                stdout=log,
                stderr=subprocess.STDOUT,
            )
            self.log(f"sampler pid {self.sampler.pid}")

    def stop_sampler(self) -> None:
        if self.sampler and self.sampler.poll() is None:
            self.sampler.terminate()
            try:
                self.sampler.wait(20)
            except subprocess.TimeoutExpired:
                self.sampler.kill()
            self.log(f"sampler pid {self.sampler.pid} stopped")

    def start_watchdog(self) -> None:
        if self.a.target != "base" or not self.guard:
            return
        if (
            subprocess.run(
                ["systemctl", "is-active", "--quiet", "ogperf-watchdog.service"], check=False
            ).returncode
            == 0
        ):
            return
        self.sh(["systemd-run", "--quiet", "--collect", "--unit=ogperf-watchdog", "-p", "Nice=10",
                 "bash", str(HERE / "watchdog.sh"), "--run", str(self.run)])  # fmt: skip

    # --- one size ---------------------------------------------------------------------------------------
    def run_size(self, homes: int, last: bool) -> None:
        self.stage(f"setup-{homes}")
        self.setup(homes)
        self.start_sampler()
        self.stage(f"warmup-{homes}")
        self.sleep(self.a.warmup_min * 60)
        self.stage(f"step-{homes}")
        self.sleep(self.a.step_min * 60)
        measured_min = self.a.step_min
        if self.a.deliver_frac > 0:
            self.stage(f"dispatch-{homes}")
            start = self.start_delivery(homes, last)
            self.sleep_until(start + timedelta(minutes=2))  # DELIVERING, and past the first ramp
            self.stage(f"deliver-{homes}")
            self.sleep(self.a.deliver_min * 60)
            measured_min = self.a.deliver_min
        if last:
            if self.a.soak_min > measured_min:
                self.stage(f"soak-{homes}")
                self.sleep((self.a.soak_min - measured_min) * 60)
            for sc in self.a.stress.split():
                self.stage(f"stress-{sc}")
                with (self.run / "logs" / f"stress-{sc}.log").open("w") as out:
                    subprocess.run(
                        [
                            sys.executable,
                            str(HERE / "stress.py"),
                            sc,
                            "--run",
                            str(self.run),
                            "--repo",
                            str(REPO),
                        ],
                        stdout=out,
                        stderr=subprocess.STDOUT,
                        check=False,
                    )
                tail = (self.run / "logs" / f"stress-{sc}.log").read_text().strip().splitlines()
                self.log(tail[-1] if tail else f"stress {sc}: no output")
                if self.aborted():
                    raise AbortedError
                self.stage(f"settle-{sc}")
                self.sleep(90)
        self.stage(f"teardown-{homes}")
        self.teardown()

    def main(self) -> int:
        steps = [int(x) for x in self.a.steps.split()]
        for n in steps:
            perfenv.fleet_shape(n)  # validate every size before starting anything
        self.log(
            f"campaign: target {self.a.target}, guard {'on' if self.guard else 'off'}, steps {steps}, warm-up "
            f"{self.a.warmup_min} min, step {self.a.step_min} min, soak {self.a.soak_min} min, stress [{self.a.stress}], "
            f"history {self.a.history.name if self.a.history else None}, deliver {self.a.deliver_frac} x "
            f"{self.a.deliver_min} min"
        )
        self.start_watchdog()
        if not self.wait_guard_clear():
            self.log("guard not clear for 2 min within 30 min -- not starting")
            return 4
        (self.run / "ABORT").unlink(missing_ok=True)
        try:
            for n in steps:
                for attempt in (1, 2):
                    try:
                        self.run_size(n, n == steps[-1])
                        break
                    except AbortedError:
                        self.log(f"size {n}: ABORTED ({(self.run / 'ABORT').read_text().strip()})")
                        self.teardown()
                        if attempt == 2 or not self.wait_guard_clear():
                            self.stage("done-aborted")
                            return 3
                        (self.run / "ABORT").unlink(missing_ok=True)
        except (RuntimeError, OSError) as exc:
            self.log(f"setup/run failure: {exc}")
            self.teardown()
            self.stage("done-failed")
            return 2
        finally:
            self.stop_sampler()
        self.stage("done")
        self.log(
            f"campaign finished; next: python tests-perf/analyze.py --run {self.run} --charts <dir> --md <file>"
        )
        return 0


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument("--target", choices=("compose", "base"), default="compose")
    p.add_argument("--run", type=Path, default=perfenv.DEFAULT_RUN)
    p.add_argument("--steps", default="1000 2500 3500 5000 7500", help="home hubs per step (<= 7500)")
    p.add_argument("--warmup-min", type=float, default=5)
    p.add_argument("--step-min", type=float, default=15)
    p.add_argument("--soak-min", type=float, default=60)
    p.add_argument("--stress", default=STRESS_DEFAULT)
    p.add_argument("--no-stress", action="store_const", const="", dest="stress")
    p.add_argument("--guard", choices=("auto", "on", "off"), default="auto")
    p.add_argument("--min-mem-mb", type=float, default=2000)
    p.add_argument("--fresh", action="store_true", help="delete <run>/ first (a new campaign)")
    p.add_argument(
        "--history",
        type=Path,
        default=HISTORY_DEFAULT if HISTORY_DEFAULT.exists() else None,
        help="og.feed_obs CSV loaded into each fresh database (default: tests-perf/.cache/ercot_history.csv if present)",
    )
    p.add_argument("--no-history", action="store_const", const=None, dest="history")
    p.add_argument(
        "--deliver-frac", type=float, default=0.5, help="share of home banks with grants (DELIVERING)"
    )
    p.add_argument(
        "--deliver-min", type=float, default=None, help="DELIVERING measured window (default: --step-min)"
    )
    p.add_argument("--no-deliver", action="store_const", const=0.0, dest="deliver_frac")
    p.add_argument(
        "--deliver-kw",
        type=float,
        default=300.0,
        help="kW per DELIVERING offer (fits banks the AS holds use in part)",
    )
    args = p.parse_args()
    if args.deliver_min is None:
        args.deliver_min = args.step_min
    if args.deliver_frac > 0 and args.history is None and args.target == "compose":
        p.error(
            "the DELIVERING regime needs --history (a fresh database has no FIRM forecast); or --no-deliver"
        )
    if args.fresh and args.run.exists():
        shutil.rmtree(args.run)
    args.run.mkdir(parents=True, exist_ok=True)
    return Campaign(args).main()


if __name__ == "__main__":
    raise SystemExit(main())
