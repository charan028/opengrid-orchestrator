# integration-sims (`ogsim`)

Independent OpenGrid integration simulators: stand-ins for the ERCOT/EIA/NWS
market and data APIs, utility SCADA, and the battery fleet, each capable of
injecting normal **and abnormal** events on demand or autonomously. This is a
separate product from the orchestrator (`opengrid`): **no code is shared**,
and neither package may import the other. See `BUILD.md` at the repo root
for the full brief, directory ownership and safety rules.

## Packages

| Package | Port | What it does |
|---|---|---|
| `ogsim.market` | 8090 | Simulates the ERCOT Public API (B2C token + 5 data products), EIA v2, and NWS. See `src/ogsim/market/README.md`. |
| `ogsim.control` | 8091 | The single control plane for every simulator: REST API, web UI, CLI, scenario runner, and the autonomous random-mode engine. See `src/ogsim/control/README.md`. |
| `ogsim.fleet` / `ogsim.scada` | - | Built by another agent; simulate the battery fleet and utility SCADA over MQTT. |

## Install

```
pip install -e integration-sims[dev]
```

## Run

```
python -m ogsim.market      # port 8090
python -m ogsim.control     # port 8091 (REST + web UI + autonomous random mode)
```

Configuration is environment-driven (see `src/ogsim/market/config.py` for
`OGSIM_MARKET_*` variables) plus `integration-sims/config/random.yaml` for
the random-mode engine.

## Test

```
.venv\Scripts\python.exe -m pytest integration-sims\tests -q
.venv\Scripts\python.exe -m pytest integration-sims\tests -q --cov=ogsim --cov-report=term-missing
```

On the server:

```
powershell -File tools\remote.ps1 -Ws mkt -Cmd "cd integration-sims && PYTHONPATH=src /opt/ogsim/venv/bin/python -m pytest -q"
```

## Code quality

```
.venv\Scripts\python.exe -m ruff format integration-sims/src integration-sims/tests
.venv\Scripts\python.exe -m ruff check integration-sims/src integration-sims/tests
.venv\Scripts\python.exe -m mypy integration-sims/src
```

Standards (enforced in `pyproject.toml`): line length 110, full type hints,
`mypy` clean, no magic numbers (named constants with units/comments),
descriptive test names, coverage >= 70%. Tests never depend on real
wall-clock sleeps or a running market/MQTT server - `conftest.py` stubs
network calls, and randomness/time are always injectable (seeded RNG,
`now`/`clock` parameters).

## Anomaly catalogue and scenarios

The full anomaly catalogue (market, SCADA, fleet - BUILD.md §3) lives in one
registry, `ogsim.control.catalogue`. Six scenario YAML files ship in
`scenarios/`. Every injection - manual (UI/CLI/REST), scenario-driven, or
autonomous random-mode - goes through `ogsim.control.injector.Injector` and
is appended to one JSONL log (`/var/lib/opengrid/sim/anomalies.jsonl`, or
`OGSIM_ANOMALY_LOG_PATH`/`./anomalies.jsonl` as fallbacks).
