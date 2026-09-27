# tests-perf -- scalability and stress harness

Owner: PERF lane. Report: `docs/orchestrator/07-delivery/17-performance-and-scalability-report.md`.
Run it on the workstation by following `HANDOFF.md`.

The harness brings up a complete, isolated OpenGrid stack: the sims plus every `og-*` process from this
checkout. It scales the simulated fleet to at most 7,500 homes, measures it, and runs the stress scenarios. It
works on two targets, chosen with `--target`:

| Target | Where | Guardrail |
|---|---|---|
| `compose` (default) | The Docker dev stack (`dev/docker-compose.yml` + `compose/docker-compose.perf.yml`) as project `ogperf`, on any Docker host | Optional (`--guard on`): the Docker VM's MemAvailable floor |
| `base` | Transient systemd units `ogperf-*` beside production (`stack.sh`, root, Linux) | Required: `watchdog.sh` samples production (cycle p99, pgdata util, fresh hubs, memory) and aborts the run |

| File | What it does |
|---|---|
| `perfenv.py` | Sizes the fleet in production proportions (hubs = homes + 9) and writes the target's configs, `.run/ports.env` and `.run/etc/secrets.env` |
| `targets.py` | Per-target process/disk/host readings, metrics scrape and broker control (cgroups + `docker exec`, or `/proc` + systemd) |
| `campaign.py` | The full run: sizes, warm-up, the IDLE and DELIVERING measured windows, soak and stress, with guard handling and a single repeat after an abort |
| `dispatch.py` | The DELIVERING regime: commits obligations through og-api's admission (demo customers, then ERCOT_ENERGY offers) until about half the home banks carry grants for a window |
| `sampler.py` | 15 s samples into `.run/data/samples.jsonl`: metrics, DB, broker `$SYS`, processes, disk, and API/UI latency |
| `stress.py` | `burst`, `outage`, `safestop`, `bulk`, `alerts`, `price`, `dbslow`; each writes `.run/data/stress-<name>.json` with PASS/FAIL checks |
| `analyze.py` | `results.json`, the per-step Markdown table, the knee fit and SVG charts (matplotlib) |
| `compose/` | The compose override and `seed_extra.py`, which runs the rest of the production seed order in `migrate` |
| `stack.sh`, `watchdog.sh` | Base target only |
| `evidence/base-2026-09-26/` | The base-server baseline records (production disk saturation, guardrail abort) |

## Reproduce

```bash
python -m venv .venv-perf && .venv-perf/bin/pip install -r tests-perf/requirements-perf.txt -r tests-perf/requirements-charts.txt
.venv-perf/bin/python tests-perf/campaign.py --fresh --guard on          # full campaign, about 3 h 45 min
.venv-perf/bin/python tests-perf/analyze.py --run tests-perf/.run --charts docs/orchestrator/07-delivery/assets/perf --md tests-perf/.run/data/steps.md
make perf-check                                                         # ruff, format, mypy, unit tests
```

Each size is measured in two regimes, because production dispatches in delivery windows and is idle between them:
- **IDLE** (`step-<homes>`): nothing delivering.
- **DELIVERING** (`deliver-<homes>`): committed obligations on about `--deliver-frac` (0.5) of the home banks.

A fresh database has no FIRM forecast, so the selector would commit nothing. `--history` (default
`tests-perf/.cache/ercot_history.csv`, git-ignored) loads 14 days of real ERCOT prices and load into each fresh
database first. `HANDOFF.md` section 1 shows how to export that file from a dev database that ran
`orchestrator/tools/ercot_backfill.py`; the campaign itself makes no live ERCOT call. `--no-deliver` measures
IDLE only.

The prerequisites, smoke run, report steps and clean-up are in `HANDOFF.md`.
