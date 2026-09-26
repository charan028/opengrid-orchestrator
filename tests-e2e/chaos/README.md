# tests-e2e/chaos -- kill-each-process runner (WORKBOARD Q3 / L4, TS-C)

Owner: qa (`tests-e2e/`). Stdlib + httpx only; never imports `opengrid`.

| File | What |
|---|---|
| `expectations.py` | The expected-behaviour table as data (`EXPECTATIONS`) and the pure `evaluate(timeline, expectation)` |
| `backends.py` | `ProcessController` protocol: `SystemdController`, `DockerComposeController`, `DryRunController` |
| `runner.py` | CLI: snapshot -> kill -> poll health -> restart -> poll -> evaluate -> Markdown report |
| `test_expectations.py`, `test_runner.py` | Hand-built timelines (engine, engine+safestop-down, guardian, api, settle) and the runner end to end |
| `NEEDS_FROM_OTHER_OWNERS.md` | What other owners must change before rows can pass for real |

## Run today, no system

```bash
orchestrator/.venv-local/bin/python tests-e2e/chaos/runner.py --backend dry-run --report /tmp/chaos-dry.md
orchestrator/.venv-local/bin/python -m pytest tests-e2e/chaos -q
```

The dry run drives the real runner against `DryRunController`, which records the kill/start calls and serves a
scripted health payload that behaves exactly as the table says a correct system does. It proves the tooling; it
proves nothing about the product.

## Run against the dev stack (docker)

```bash
make dev-up                                            # WORKBOARD D0 (lead)
orchestrator/.venv-local/bin/python tests-e2e/chaos/runner.py --backend docker \
    --compose-file dev/docker-compose.yml --api-base http://127.0.0.1:8080 \
    --report tests-e2e/chaos/reports/dev.md
```

Assumes compose services named like the systemd units (`og-engine`, ...; `postgresql`, `mosquitto` for infra) and
`docker compose -f <file> kill|start <service>`. If D0 names them differently, override
`DockerComposeController.kill_cmd/start_cmd/ps_cmd` (they are `{file}`/`{service}` templates) -- no other code changes.

## Run on the server (lead, systemd, as root)

`systemctl kill` needs root and there is no sudo on basepower (`deploy/README.md`), so this is the one e2e script
that runs as root, over the deploy role's SSH key, never via `tools/remote.ps1`:

```bash
python3 tests-e2e/chaos/runner.py --backend systemd --report /var/lib/opengrid/chaos-$(date +%F).md
python3 tests-e2e/chaos/runner.py --backend systemd --only engine --report /tmp/engine.md
python3 tests-e2e/chaos/runner.py --backend systemd --include-infra --report /tmp/full.md   # also postgres, mosquitto
```

`SystemdController.kill` is `systemctl kill --signal=SIGKILL og-<x>` (the unit stays "active", `Restart=always`
would restart it after `RestartSec=2` anyway); the runner still calls `systemctl start` so recovery has a defined
t=0. Never run `--include-infra` while anything else is using the host's Postgres or Mosquitto.

## What is expected, and where it comes from

Numbers: `[health] heartbeat_interval_s = 5`, `heartbeat_miss_threshold = 3` -> a process is `down` after 15 s
(`health/rules.py classify_process_status`); the runner allows 15 s of evaluator/poll slack -> **30 s**. Recovery:
`RestartSec=2` + start-up + first heartbeat + one evaluator cycle -> **30 s** (60 s for infra, 60 s for the sim
because hubs must come back online). Degraded modes: `health/rules.py derive_degraded_modes`. Invariant counters
(`reserve_breaches`, `double_sold_kwh`) must read 0 in every observation (A10, K1/K2).

| process | unit | down within | degraded modes | eventual | must stay ok | alerts | recovery | behaviour / source |
|---|---|---|---|---|---|---|---|---|
| feeds | og-feeds | 30 s | - | NO_NEW_COMMITMENTS | engine, guardian, safestop, settle, api | ALR-PROCESS-DOWN | 30 s | Engine allocates on last-good values; `NO_NEW_COMMITMENTS` only once a feed crosses STALE (`[feeds.staleness] ercot_price_fresh_s = 600`), outside the run's window, so documented not asserted. |
| engine | og-engine | 30 s | HOLD_LOCAL_AUTONOMY | - | guardian, safestop, feeds, settle, api | ALR-PROCESS-DOWN | 30 s | Hubs hold the last setpoint until lease expiry (`[fleet] lease_ttl_s = 30` + `fleet.yaml lease_hold_after_expiry_s = 5`), then local autonomy. **safestop must stay ok: K8** (`safestop/README.md`, `og-safestop.service` has no After=/Requires= on engine/guardian). |
| guardian | og-guardian | 30 s | HOLD | - | engine, safestop, feeds, settle, api | ALR-PROCESS-DOWN | 30 s | Engine keeps ticking, proposes no batches (`engine/README.md` step 5); K3. safestop stays ok (K8). |
| safestop | og-safestop | 30 s | - | - | all others | ALR-PROCESS-DOWN | 30 s | No degraded mode; stop authority unavailable until restart. |
| sim | og-sim-fleet | 30 s | - | - | all 6 og-* | ALR-PROCESS-DOWN, ALR-HUB-OFFLINE-RATIO | 60 s | No telemetry: hubs stale > 6 s, offline > 30 s (`classify_hub_health`), offline-ratio alert critical. Outage is held >= 45 s so this is observable. |
| settle | og-settle | frozen | - | - | all others | - | 30 s | The evaluator runs inside og-settle (`health/README.md`), so alerts and hub counts freeze and nobody can be marked down. Recovery = settle's heartbeat `ts` advancing again. |
| api | og-api | unreachable <= 5 s | - | - | engine, guardian, safestop, feeds, settle | - | 30 s | `/og/api/health` refuses connections for the whole outage; on the first reachable recovery snapshot every other process is ok and its heartbeat `ts` advanced (engine kept ticking). |
| postgres | postgresql | unreachable <= 5 s | - | - | - | - | 60 s | Every pool is gone; nothing may crash-loop (`platform/heartbeat.py` never raises, `run_forever` K7); all 7 read ok again unaided. Infra: `--include-infra` only. |
| mosquitto | mosquitto | health reachable | - | - | all 6 og-* | ALR-HUB-OFFLINE-RATIO | 60 s | Heartbeats are via Postgres so processes stay ok; hubs go offline after 30 s, hold then local autonomy after the lease. Infra: `--include-infra` only. |

Generic checks on every row: `processes[x].status`, `degraded_modes`, `must_stay_ok` status **and** heartbeat `ts`
advancing (a process that is "ok" but stopped writing heartbeats is caught), open alert rules, zero counters,
recovery time. For `api`/`postgres` the runner additionally fails if health becomes reachable mid-outage.

## When a row fails

1. Open the row's section in the report: each check names what it compared and the observed value, and the
   `observation timeline` lists every health poll (processes not ok, `degraded_modes`, open alert rules, errors).
2. `degraded_modes missing from the health payload`, `status` never flipping to `down`, or settle never being
   marked anything: these are the known product gaps in `NEEDS_FROM_OTHER_OWNERS.md`, not test bugs -- hand the
   report to that owner.
3. `[...] stay ok` failing on the **engine** or **guardian** row with `safestop` listed is a K8 violation: blocks
   (BUILD.md S6). Check `og-safestop.service` dependencies and `journalctl -u og-safestop`.
4. `reserve_breaches`/`double_sold_kwh` non-zero anywhere is A10/K1/K2: blocks. Capture `og_reserve_breaches_total`
   from the guardian's `/metrics` and the `og.trace` rows around the kill time.
5. Recovery too slow: `journalctl -u og-<x> --since -5min` -- usually a start-up dependency (DB pool, MQTT auth)
   rather than the process itself; compare against `RestartSec=2`.
6. Re-run only that row with `--only <process>` before filing; a single slow poll can push a 30 s limit.
