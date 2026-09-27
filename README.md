# OpenGrid Orchestrator (MVP-S)

Read `BUILD.md` first -- it fixes scope, ownership and the code-quality gate (S5a). This repository
holds two independent products that share no code and meet only at the wire (`interfaces/`):

- `orchestrator/` -- package `opengrid`, the live app (engine, guardian, safestop, settle, feeds, api).
- `integration-sims/` -- package `ogsim`, the integration simulators (hubs, SCADA, market/data APIs).

## Quick start (local, no DB/MQTT)

```
.venv\Scripts\python.exe -m pytest orchestrator\tests\unit -q
```

## Full pre-merge gate

```
make check          # lint + typecheck + dupcheck + test-unit + coverage
```

or, on the server workspace:

```
powershell -File tools\remote.ps1 -Ws <ws> -Cmd "cd orchestrator && bash tools/check.sh"
```

## Layout

See `orchestrator/INTERFACES.md` for the module interface index, `interfaces/` for the language-neutral
wire contracts, and `docs/orchestrator/07-delivery/` (in this repo) for the approved specs.
