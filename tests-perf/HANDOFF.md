# HANDOFF: run the scalability and stress campaign on the workstation

From the PERF lane, 2026-09-26. Owner decision: the campaign runs on the workstation's Docker dev stack,
not on the base server, once r3.4.1 has been delivered. Base is off limits for this work: do not start
anything there.

The deliverable is `docs/orchestrator/07-delivery/17-performance-and-scalability-report.md` plus SVG charts in
`docs/orchestrator/07-delivery/assets/perf/`. Start the report from Appendix A below; its base-server
baseline section is already written and must stay in.

## 0. What you get

- **Scalability:** steady state at 1,000 / 2,500 / 3,500 / 5,000 / 7,500 homes (1,009 to 7,509 hubs, production
  shape: 50 homes per bank, 20 % dual-unit, AEN/LCRA/RAYBN zone blocks, substation asset, 8 trucks). Each size
  gets a fresh database, a 5 min warm-up and a 15 min measured window.
- **Soak:** the 7,500 size runs for 60 min in total (its 15 min window plus 45 min more) to check memory growth.
- **Stress at 7,500:** `price` (10 min price spike on every zone), `bulk` (500-hub manual command, at the cap),
  `alerts` (storm, then bulk ack), `burst` (broker restart, every client reconnects at once), `outage`
  (60 s broker outage), `dbslow` (ACCESS EXCLUSIVE lock on og.trace for 60 s; fail-closed check), `safestop`
  (fleet-wide stop until every hub is at 0 kW and the stop outbox drains, then the two-person release).
- **Measured throughout:** engine cycle p50/p95/p99/max and per-phase p99 (CYCLE_LATENCY trace and the tick
  histogram); guardian verdict latency and G-20 timeouts; telemetry ingest rate and lag; hub freshness; MQTT
  msgs/s; Postgres commits, rows, WAL, growth and disk; CPU and RSS per container; API p95 for the fleet
  table, map, alerts and health, and the `/og/fleet` page.

**Expected duration:** about 3 h 45 min unattended.

| Part | Time |
|---|---|
| Sizes | 5 × (about 3–5 min build/seed + 5 warm-up + 15 measured) ≈ 2 h |
| Soak | 45 min extra |
| Stress | about 40 min |
| First image build | 5–10 min extra |

A smoke run (section 3) takes about 12 min.

## 1. Prerequisites (once)

- Docker with Compose v2: Docker Desktop on Windows or macOS, or Docker Engine on Linux. Give the Docker VM at
  least 8 CPUs and 12 GB RAM. The earlier dev-stack scale note used 24 threads / 32 GB.
- A checkout of branch `integ/perf-scale` (or the r3.4.1 tag once it includes it), with a bash for the `dev/`
  make targets (Git Bash on Windows).
- The dev stack must have worked once, so that `dev/secrets`, `dev/.env`, `dev/keys/*-dev.{key,pub}` and
  `dev/mosquitto/opengrid.acl` exist:
  ```bash
  make -C dev secrets .env dev-acl dev-keys     # Windows alternative for keys: dev/scripts/gen-keys.ps1
  ```
  Fill every value in `dev/secrets` with local-only values. `POSTGRES_PASSWORD` must equal `OG_DB_PASSWORD`,
  and `OG_API_PROXY_SECRET` must be set.
- The harness venv (Python 3.13):
  ```bash
  python -m venv .venv-perf
  .venv-perf/bin/pip install -r tests-perf/requirements-perf.txt -r tests-perf/requirements-charts.txt
  # Windows: .venv-perf\Scripts\pip ...   and use .venv-perf\Scripts\python below
  ```
- Stop your normal dev stack. The perf project `ogperf` publishes the same host ports (5432, 1883, 8080, 8090):
  `docker compose -f dev/docker-compose.yml --profile orchestrator stop`.
- Record the environment for the report:
  ```bash
  docker version; docker info --format '{{.NCPU}} CPUs, {{.MemTotal}} bytes, {{.OperatingSystem}}, {{.KernelVersion}}'
  git rev-parse --short HEAD
  ```
  Also record the workstation CPU model, core count, RAM, disk type (NVMe/SSD) and host OS.

## 2. Gate the harness

```bash
cd tests-perf
../.venv-perf/bin/python -m ruff check . && ../.venv-perf/bin/python -m ruff format --check .
../.venv-perf/bin/python -m mypy --config-file mypy.ini perfenv.py sampler.py stress.py analyze.py targets.py campaign.py
../.venv-perf/bin/python -m pytest -q -p no:cacheprovider test_perf_harness.py
cd ..
```

## 3. Smoke run (about 12 min)

```bash
.venv-perf/bin/python tests-perf/campaign.py --fresh --steps "1000" --warmup-min 2 --step-min 3 --soak-min 0 --no-stress
.venv-perf/bin/python tests-perf/analyze.py --run tests-perf/.run --md tests-perf/.run/data/steps.md
```

Expect `seeded: og.hub = 1009` in `tests-perf/.run/logs/campaign.log`, a `step-1000` column in `steps.md` with
cycle, verdict, telemetry and API values, and no `n/a` in the engine rows. `tests-perf/.run/logs/compose.log`
holds the build and compose output. If the `engine` rows are `n/a`, check that `docker exec` works for the
og-engine container; the sampler scrapes its loopback-only `/metrics` that way.

## 4. Full campaign

```bash
.venv-perf/bin/python tests-perf/campaign.py --fresh --guard on
```

- `--guard on` enables the local guard: it aborts if the Docker VM's MemAvailable drops below 2 GB
  (`--min-mem-mb`). The production watchdog applies only to `--target base`.
- Progress is in `tests-perf/.run/logs/campaign.log`, and the current stage in `tests-perf/.run/STAGE`.
- On an abort the campaign repeats that size once after the guard has been clear for 2 min. A second abort ends
  it. Every abort is in `tests-perf/.run/aborts.jsonl` and must go in the report.
- Useful variants: `--steps "7500" --soak-min 60` (7,500 + soak + stress only), `--stress "safestop dbslow"`,
  and `--no-stress`.

## 5. Produce the report and charts

```bash
.venv-perf/bin/python tests-perf/analyze.py --run tests-perf/.run \
    --charts docs/orchestrator/07-delivery/assets/perf --md tests-perf/.run/data/steps.md
cp tests-perf/.run/data/results.json docs/orchestrator/07-delivery/assets/perf/results.json
```

Create `docs/orchestrator/07-delivery/17-performance-and-scalability-report.md` from Appendix A. Then fill in the
report:

1. **Environment:** the workstation hardware, Docker version and VM limits (section 1). State plainly that
   this is not the base server, and that all simulators and the database share the machine with the
   orchestrator.
2. **Versions:** the commit you ran and the r3.4 / r3.4.1 SHAs.
3. **Results per step:** paste `steps.md`.
4. **Knee:** `results.json` → `knee`. That's the quadratic fit of p99 against hubs: the budget crossing and the
   10k projection. Also name the first saturating resource: engine CPU close to 100 % of one core, the sim-fleet
   core, DB disk util, or MemAvailable.
5. **Stress:** each `results.json` → `stress.<name>`, with its PASS/FAIL checks as recorded. Report a FAIL as a
   FAIL, with the numbers.
6. **Soak:** `results.json` → `soak`: RSS at start and end and the slope per process in MB/h. Any restart shows
   as a PID change.
7. **Guardrail events:** the base abort (already in the template) plus any workstation aborts.
8. **Bottlenecks and recommendations** from the data: the per-phase chart shows where the cycle goes.

Charts written by `analyze.py`: `cycle-latency-vs-fleet.svg`, `cycle-phases-vs-fleet.svg`,
`cpu-per-process-vs-fleet.svg`, `db-write-load-vs-fleet.svg`, `api-p95-vs-fleet.svg`, `soak-rss.svg`.

Commit the report, the charts and `results.json` (no `.run/`, no `compose/generated/`, both gitignored), gate
as in section 2, push, and send the tip to the release manager.

## 6. Clean up

```bash
docker compose -p ogperf -f dev/docker-compose.yml -f tests-perf/compose/docker-compose.perf.yml --profile orchestrator down -v
rm -rf tests-perf/.run tests-perf/compose/generated
docker compose -f dev/docker-compose.yml --profile orchestrator up -d     # your normal dev stack, if wanted
```

## 7. How it works (compose target)

- `perfenv.py --target compose --homes N` writes `tests-perf/compose/generated/`:
  - sim configs for N homes;
  - a config directory whose `orchestrator.toml` is production's tuning (`orchestrator/config/orchestrator.toml`)
    with the dev stack's hosts, feeds (`sim-market`), keys and binds (`dev/config/docker.toml`);
  - the dev ACL, with `$SYS` read added for `og_simctl`, the harness's monitor user.

  It also writes `tests-perf/.run/{ports.env,etc/secrets.env}`, taking the values from `dev/secrets`.
- `compose/docker-compose.perf.yml` layers those files onto `dev/docker-compose.yml` as project `ogperf`. It also
  makes `migrate` run the full production seed order: `compose/seed_extra.py` loads the market model, customer
  services, services, topology and trucks after the fleet.
- `targets.py` reads CPU and memory from each container's cgroup, disk counters from the Docker VM (inside the
  postgres container), and og-engine/og-guardian metrics through `docker exec`.
- Differences from production that the report must state:
  - one machine runs everything;
  - the Docker VM disk (usually SSD/NVMe) replaces base's virtual HDD, so DB disk figures do not transfer to
    base; project them from WAL MB/h and row rates instead;
  - `dev/config/docker.toml` connectivity, with no Apache in front of the API (the harness calls og-api directly
    with the proxy secret, so the API numbers exclude Apache and TLS).

## 8. Known risks

- `sim-fleet` is one Python process. Earlier dev-stack notes saw it saturate one core at 10k hubs with a 2 s
  cadence. The cadence is now 10 s and the scope 7,500, but if its CPU reads close to 100 %, the load generator
  is the limit, not the orchestrator. Say so in the report and do not count it as an orchestrator failure.
- `safestop` leaves the fleet stopped if the two-person release fails. The stack is torn down after the stress
  phase, so the next run is unaffected. The release result is recorded either way.

## Appendix A: report skeleton (docs/orchestrator/07-delivery/17-performance-and-scalability-report.md)

Copy everything between the two `----8<----` lines into the report file. Replace every FILL marker with measured
values and keep section 2 as written. Report failures as failures.

----8<----

# Performance and scalability report: 1,000 to 7,500 homes (r3.4)

Status: FILL (date, who ran it, commit).

## 1. Summary

FILL: three to five sentences. Give the knee (the fleet size where cycle p99 crosses 500 ms, or the first
saturating resource), the headroom at production's 3,509 hubs and at 7,509, the stress verdicts, and the 10k
outlook.

## 2. Baseline evidence from the base server (2026-09-26, before the campaign)

The campaign was first set up on the base server (192.168.5.35), beside production r3.4 `b1722df` (3,509 hubs).
It was stopped there, and the owner moved it to the workstation. What the base attempt showed about production
itself matters for capacity planning. Raw records are in `tests-perf/evidence/base-2026-09-26/`.

- **Production's database disk saturates without any test load.** `sar -d` 10-minute averages put pgdata
  (dm-6) at 98.3 % util at 21:10 and 99.8 % at 21:40 CDT, with no perf work running. The disk is a QEMU virtual
  HDD with a write-back cache. pgdata, the test cluster and root are logical volumes on that one device, and its
  I/O scheduler was `none`, so `ionice`/`IOWeight` gave no isolation. The scheduler has since been changed to
  bfq.
- **The production engine stalls every few minutes.** The og-engine `lifecycle` phase blocked the event loop
  for 3.5–5.4 s about every 5 minutes (4,237 ms at 21:57, 5,384 ms at 22:02 CDT). Loop lag equals the phase
  time, so this is synchronous work on the event loop. The PROD-IO lane attributed it to the PQ characterization
  loop. The A11 rolling p99 (139–296 ms) hides these stalls because they are under 1 % of cycles, but the
  cycle max shows them.
- **Guardrail event.** A 1,000-home isolated perf stack ran from 22:06:30 to 22:08:16 CDT (perf cycle p99
  50 ms). At 22:08:13 the watchdog aborted on pgdata util of 100 %, and every perf process was stopped by exact
  unit and PID within 3 s. Production then stayed saturated for at least 7 more minutes with no perf process
  running: pgdata at 100 % util, w_await 100–740 ms, device flush await 2.1–2.8 s. Over the same window the
  test-cluster device showed 0 writes/s and dirty memory was 1.8 MB. Production cycle p99 reached 2.9–4.7 s
  (max 14.7 s), while fresh hubs stayed at 3,509.
- **Implication.** On base today, the first saturating resource is production's disk, at the current 3,509 hubs,
  before any orchestrator CPU limit. Any 7,500- or 10,000-hub plan for base needs a faster disk (or separate
  disks for WAL and data) and the lifecycle/PQ-characterization work moved off the event loop, whatever the
  workstation results show.

## 3. Environment and method

- **Machine:** FILL (CPU model, cores/threads, RAM, disk type, host OS). Docker FILL, VM limits FILL CPUs /
  FILL GB. This is not the base server: the simulators, Postgres, Mosquitto and all six og-* processes share the
  one machine.
- **Stack:** `dev/docker-compose.yml` with the orchestrator profile, plus `tests-perf/compose/docker-compose.perf.yml`,
  as project `ogperf`. The config is production's `orchestrator.toml` tuning with the dev stack's connectivity.
  The API is called directly (no Apache or TLS in front).
- **Fleet shape:** production-proportioned (`tests-perf/perfenv.py`): 50 homes per bank, 20 % dual-unit, zone
  blocks LZ_AEN/LZ_LCRA/LZ_RAYBN scaled with the fleet, plus the substation asset and 8 trucks. Telemetry every
  10 s.

  | Homes | Hubs | Banks |
  |---:|---:|---:|
  | 1,000 | 1,009 | 20 |
  | 2,500 | 2,509 | 50 |
  | 3,500 | 3,509 (= production) | 70 |
  | 5,000 | 5,009 | 100 |
  | 7,500 | 7,509 | 150 |
- **Method:** each size gets a fresh database and the full production seed order, a 5 min warm-up, then a
  15 min measured window. The 7,500 size continues into the soak (60 min total), then the stress scenarios.
  Samples are taken every 15 s, with API probes every 60 s (10 requests per endpoint).
- **Sources:**
  - cycle p50/p99/max and per-phase p99 from the CYCLE_LATENCY trace;
  - p95 from the tick histogram (bucket-interpolated);
  - verdict latency and G-20 timeouts from GUARDIAN_VERDICT traces.

## 4. Versions

FILL: harness commit, r3.4 `b1722df`, r3.4.1 FILL, Docker FILL, Postgres 17, Mosquitto 2.

## 5. Results per step

FILL: paste `tests-perf/.run/data/steps.md`.

![Engine cycle latency vs fleet size](assets/perf/cycle-latency-vs-fleet.svg)
![Per-phase p99](assets/perf/cycle-phases-vs-fleet.svg)
![CPU per process](assets/perf/cpu-per-process-vs-fleet.svg)
![Postgres write load](assets/perf/db-write-load-vs-fleet.svg)
![API p95](assets/perf/api-p95-vs-fleet.svg)

## 6. Knee and capacity headroom

FILL: the budget crossing in hubs and p99 at 10k (`results.json` → `knee`), the first saturating resource,
and the headroom at 3,509 and 7,509 (p99 margin to 500 ms, CPU margin of the busiest process).

## 7. Stress results (at 7,509 hubs)

| Scenario | Criteria | Result | Key numbers |
|---|---|---|---|
| Telemetry burst (broker restart) | every hub publishing again; all fresh in DB <= 60 s; p99 < 500 ms within 2 min | FILL | FILL |
| Fleet-wide safe stop | confirm 200; every hub at 0 kW <= 30 s; stop_outbox drained; two-person release accepted | FILL | FILL |
| Bulk command at the 500 cap | propose < 5 s; RAMPING; 95 % follow <= 5 min; p99 < 500 ms | FILL | FILL |
| Alert storm + bulk ack | alerts raised; list(500) < 2 s; each ack(500) < 10 s; p99 < 500 ms | FILL | FILL |
| Price spike | spike reached og.feed_obs; p99 < 500 ms | FILL | FILL |
| 60 s broker outage | hubs back; all fresh <= 60 s; engine cycling | FILL | FILL |
| DB slow-down (og.trace locked 60 s) | no command published while locked (fail closed); commands resume; p99 < 500 ms within 2.5 min | FILL | FILL |
| 1 h soak | no memory growth (RSS slope per process), no restarts | FILL | FILL |

![Soak memory](assets/perf/soak-rss.svg)

## 8. Guardrail events

- Base server, 22:08:13 CDT: abort on production pgdata util of 100 % (section 2).
- Workstation: FILL (from `tests-perf/.run/aborts.jsonl`, or "none").

## 9. Bottlenecks

FILL, from the per-phase and CPU charts.

## 10. Recommendations

FILL. Cover:
- the retry phase;
- batching of telemetry and hub_state writes;
- indexes and partitioning for telemetry and trace;
- moving the lifecycle/PQ characterization off the event loop;
- base disk capacity (section 2);
- the 10k outlook.

## 11. How to reproduce

See `tests-perf/HANDOFF.md` and `tests-perf/README.md`.

----8<----
