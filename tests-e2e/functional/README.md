# Functional e2e suites (Q1, Q2)

Black-box scenarios from `04-mvp-s-test-plan.md`, run against the local dev stack (`dev/`, WP D0). Tests act
through the operator API and the `ogsim.control` anomaly plane, and read `og.*` rows only to assert on what the
running processes did.

| Suite | WP | Scenarios |
|---|---|---|
| `dispatch/test_ts04_commitment_lock.py` | Q1 | TS-04-05, -06, -15, -16 (gate-level, ~1 min) |
| `dispatch/test_ts04_delivery.py` | Q1 | TS-04-07, -11/-13, -12, TS-05-07 (`slow`: waits for a real delivery window) |
| `safety/test_ts06_guardian_and_safe_stop.py` | Q2 | TS-06-07a, -09, -15, -16, -17, -23, two-person release, TS-07-04, A11; an R3 manual discharge target reached and held, measured (D-38, `slow`) |
| `decisions/test_d12_k7_as.py` | Q4 | D-12 two-person release by og-op-a/og-op-b, K8 replay of an old RELEASE, K7 veto-rate escalation |
| `decisions/test_d17_d18_delivery.py` | Q4 | D-17 best-effort SHORTFALL under an L2 BLOCK and its lift; D-18 need-basis G-19 (`slow`) |
| `decisions/test_as_capacity_hold.py` | Q4 | ERCOT_AS capacity hold: 0 kW until an ERCOT deployment, then back to the hold; the deployment's delivered power, measured (D-38) (`slow`) |
| `r3/test_utility_toll.py` | R3 | D-29 toll: daily window, 0 kW hold, a call discharges it and its delivered power is measured (D-38, `slow`, only inside 16:30-18:00 CT), 90-minute cap, ERCOT deployments never touch it, availability settlement |
| `r3/test_ercot_as_poll.py` | R3 | D-35 ERCOT AS instructions: refusals, and an ECRS deployment, its duplicate and its recall, with its delivered power measured (D-38, `slow`); needs `OG_E2E_ERCOT_AS_POLL=1` |

The delivered-power checks (D-38) share `delivery_check.py`: hub telemetry judged by `opengrid.core.delivery`,
and on r3.4.3 stacks og-settle's own `og.delivery_record` for the same call. `conftest.py` puts this checkout's
`orchestrator/src` first on `sys.path`, so the suites judge with the code under test, never with another
checkout a venv may have installed.

## Run

```bash
docker compose -f dev/docker-compose.yml --profile orchestrator up -d
pytest tests-e2e/functional -v                 # everything
pytest tests-e2e/functional -v -m "not slow"   # skip the scenarios that wait minutes for a delivery
```

Every test skips (not fails) when the stack is not reachable. Defaults read `dev/secrets` and `dev/.env`;
override with `OG_E2E_API`, `OG_E2E_CONTROL`, `OG_E2E_DSN`, `OG_E2E_PROXY_SECRET`, `OG_E2E_INVERTER_CAP_KW`.

`r3/test_broker_recovery.py` scrapes each service's loopback `/metrics` with `docker compose exec`. It reads the
ports from `[metrics]` of `dev/config/docker.toml`, the file the stack's og-* services load. Set `OG_E2E_CONFIG`
for a stack that loads another file. A port not set there falls back to the service's own default: og-guardian
`guardian_port` 9103, og-safestop `safestop_port` 9106. og-engine serves no `/metrics` without `engine_port`.
`OG_E2E_ENGINE_METRICS_PORT`, `OG_E2E_GUARDIAN_METRICS_PORT` and `OG_E2E_SAFESTOP_METRICS_PORT` override a port.

## Dev-stack settings these suites need

As of `main` @ `e86cef0` the stack needs these local settings to run the orchestrator profile at all (reported to
the lead; none are committed here, `dev/` is not this WP's path):

- `dev/secrets`: `OG_API_PROXY_SECRET=<any value>` (og-api refuses identity headers without it) and
  `OGSIM_ENV=prod` (the sims refuse the `og/v1` topic root otherwise).
- `dev/docker-compose.yml`: drop the `./secrets.example` `env_file` entries (its empty values override
  `dev/secrets`).
- `dev/config/docker.toml`:
  - `[api] allow_non_loopback_bind = true` (og-api runs in a container);
  - `[feeds.ercot] token_url = "http://sim-market:8090/token"`, plus the simulator's test credentials for
    og-feeds (otherwise it logs in to the real ERCOT B2C endpoint);
  - for the two-person release scenarios, the D-12 test operators `og-op-a` and `og-op-b` (already in
    `orchestrator/config/orchestrator.toml` `[api.roles] operator` and `[guardian] stop_release_authorised_operators`;
    override with `OG_E2E_OPERATOR_A` / `OG_E2E_OPERATOR_B`), and
    `[safestop] guardian_public_key_path = "/app/dev/keys/guardian-dev.pub"` (without it og-safestop refuses to
    relay a guardian-signed RELEASE: `GUARDIAN_PUBLIC_KEY_NOT_CONFIGURED`).
- `dev/.env`: `POSTGRES_PORT` if another Postgres already owns 5432 on the host.

## Isolation

Committed obligations stay locked (K13) even after their contract ends, so each scenario uses fresh contracts and
windows with no existing commitments (`Stack.free_window`). A long-lived dev database fills up; reset it with
`docker compose -f dev/docker-compose.yml down -v` when `free_window` reports no free intervals.

`bank-007` is the one bank the safety suite stops; the two-person release scenarios release it again.

The engine dispatches uncommitted headroom too, so on a busy stack every hub may be under dispatch. A manual command
on such a hub can be refused for reasons unrelated to the scenario (G-13 lease/sequence), so the safety assertions
check the rule under test rather than demanding PASS, and the one PASS scenario skips when no hub is idle.

## Known failure (xfail, strict)

None. The former `bank_overload` xfail (A11: the injection never lifted a bank over its kVA rating) is fixed by
#43 B1: the SCADA simulator now reports `rating x (1 + kva_over_rating_pct / 100)` while the anomaly is active.
