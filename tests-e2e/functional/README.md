# Functional e2e suites (Q1, Q2)

Black-box scenarios from `04-mvp-s-test-plan.md`, run against the local dev stack (`dev/`, WP D0). Tests act
through the operator API and the `ogsim.control` anomaly plane, and read `og.*` rows only to assert on what the
running processes did.

| Suite | WP | Scenarios |
|---|---|---|
| `dispatch/test_ts04_commitment_lock.py` | Q1 | TS-04-05, -06, -15, -16 (gate-level, ~1 min) |
| `dispatch/test_ts04_delivery.py` | Q1 | TS-04-07, -11/-13, -12, TS-05-07 (`slow`: waits for a real delivery window) |
| `safety/test_ts06_guardian_and_safe_stop.py` | Q2 | TS-06-07a, -09, -15, -16, -17, -23, two-person release, TS-07-04, A11 |

## Run

```bash
docker compose -f dev/docker-compose.yml --profile orchestrator up -d
pytest tests-e2e/functional -v                 # everything
pytest tests-e2e/functional -v -m "not slow"   # skip the delivery-window scenarios
```

Every test skips (not fails) when the stack is not reachable. Defaults read `dev/secrets` and `dev/.env`;
override with `OG_E2E_API`, `OG_E2E_CONTROL`, `OG_E2E_DSN`, `OG_E2E_PROXY_SECRET`, `OG_E2E_INVERTER_CAP_KW`.

## Dev-stack settings these suites need

As of `main` @ `b5f17a9` the stack needs these local settings to run the orchestrator profile at all (reported to
the lead; none are committed here, `dev/` is not this WP's path):

- `dev/secrets`: `OG_API_PROXY_SECRET=<any value>` (og-api refuses identity headers without it) and
  `OGSIM_ENV=prod` (the sims refuse the `og/v1` topic root otherwise).
- `dev/docker-compose.yml`: drop the `./secrets.example` `env_file` entries (its empty values override
  `dev/secrets`).
- `dev/config/docker.toml`:
  - `[api] allow_non_loopback_bind = true` (og-api runs in a container);
  - `[feeds.ercot] token_url = "http://sim-market:8090/token"`, plus the simulator's test credentials for
    og-feeds (otherwise it logs in to the real ERCOT B2C endpoint);
  - for the two-person release scenarios: `[api.roles] operator = ["e2e-alice", "e2e-bob"]` and
    `[guardian] stop_release_authorised_operators = ["e2e-alice", "e2e-bob"]`.
- `dev/.env`: `POSTGRES_PORT` if another Postgres already owns 5432 on the host.

## Isolation

Committed obligations stay locked (K13) even after their contract ends, so each scenario uses fresh contracts and
windows with no existing commitments (`Stack.free_window`). A long-lived dev database fills up; reset it with
`docker compose -f dev/docker-compose.yml down -v` when `free_window` reports no free intervals.

`bank-007` is the one bank the safety suite stops. Until the release bug below is fixed it stays stopped.

## Known failures (xfail, strict)

- **Two-person safe-stop release never succeeds.** og-api writes the `SAFE_STOP_RELEASE` row only at approval,
  with `confirmed_at` taken just before the insert; og-guardian reads the row's `created_at` as `requested_at`, so
  approval always predates the request and every release is refused `APPROVAL_STALE`.
- **`bank_overload` injection has no effect** on the SCADA simulator's readings, so `ALR-SCADA-OVERLOAD` cannot be
  exercised on the dev stack.
