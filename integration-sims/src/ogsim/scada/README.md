# ogsim.scada

Simulates utility SCADA per bank/feeder (02b §4.2, §5.1): aggregates fleet
telemetry into a per-bank kVA reading plus background load, issues
rule-based or scenario-driven utility instructions, and injects the
SCADA_* anomaly catalogue.

## Purpose

- `aggregation.py` -- per-bank real-power aggregation + kW->kVA conversion
  (fixed assumed power factor, documented constant).
- `background.py` -- feeder background load, seeded from
  `mariadb_history_signals.tsv`'s `substation_load_kw` when present on the
  server, else a synthetic diurnal curve (never crashes if absent).
  `substation_load_kw` is the whole feeder's (all `bank_count` banks')
  aggregate reading, so it is divided by `bank_count` to get one bank's
  share -- skipping that division was a defect that replayed the whole
  substation's ~3,000 kW history mean onto every bank independently
  (~3,000 kVA reported against a 600 kVA rating, 40 false
  `ALR-SCADA-OVERLOAD` alerts on the live server).
- `instructions.py` -- consecutive-overload counter -> auto LIMIT rule.
- `anomalies.py` -- SCADA_* anomaly apply/revert.
- `runtime.py` -- `ScadaEngine` (pure per-tick logic) + `run_scada` (async
  MQTT shell).

## Interface

`python -m ogsim.scada` connects to MQTT, subscribes to `<root>/tel/#` and
`<root>/scenario/cmd`, and publishes `<root>/scada/<bank_id>` every
`publish_interval_s` plus `<root>/scada/instruction/<bank_id>` on rule
trigger or scenario command.

**Known wire-shape compromise**: same `<root>/scenario/cmd` tolerant
parsing as `ogsim.fleet` -- see `ogsim.common.scenario`'s docstring.

## How to test

```
.venv\Scripts\python.exe -m pytest integration-sims\tests -k scada -q
```
