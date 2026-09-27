# dev/seed: the data a fresh OpenGrid database needs

No migration or deploy step runs these files. `deploy/scripts/bootstrap_from_scratch.sh` phase e applies them in
the order below (`deploy/BOOTSTRAP.md`, section 0), and `deploy/scripts/bootstrap_check.py` then asserts the
result. Every seed is idempotent: re-running converges and never duplicates a row. None of them overwrites live
telemetry (`og.hub_state` rows are insert-only) or a committed obligation.

## Order

| # | Seed | Writes | Needs first |
|---|---|---|---|
| 0 | migrations `0001`-latest | schema; the demo contracts (0002, 0022); the FLEET charge window `22:00-06:00` (0038) | an empty database |
| 1 | `python -m opengrid.fleet.seed` with `OG_FLEET_SIM_CONFIG=/etc/opengrid/sim/fleet.yaml` | `og.bank`/`og.hub`: the base 2,000 hubs / 40 banks (4 competitive zones) plus every enabled zone block (LZ_AEN: 500 hubs / 10 banks). 20 % of each bank's homes are dual-unit (78.4 kWh / 20 kW), the rest 39.2 kWh / 11 kW | 0; the generated sim config (`deploy/scripts/gen_sim_overrides.py`) |
| 2 | `market_model_seed.sql` | `og.utility` AUSTIN_ENERGY ($102/kW-yr) and CPS_ENERGY; the Austin tolling contract (REGULATED_CAPACITY/TOLLING, 24 MW, 90 min); the 20 MW / 2 h substation set `sub-LZ_AEN-00` (its bank, hub, feeder and substation limits, `og.asset` ACTIVE) | 1 with LZ_AEN enabled |
| 3 | `customer_services_seed.sql` | the DATA_CENTER and PIPELINE_AC customers and contracts (`og-cust-dc`, `og-cust-pipe`) | 0 (0025, 0026) |
| 4 | `services_seed.sql` | the PJM_CAPACITY, MOBILE_STORAGE and LARGE_LOAD customers and contracts (`og-cust-pjm`, `-mobile`, `-largeld`) | 0 (0025) |
| 5 | `mobile_trucks_seed.sql` (TRUCKS lane; applied when present in the release) | the mobile truck units | 0 (0044) |
| 6 | `topology_seed.py --fleet-config /etc/opengrid/sim/fleet.yaml --scada-config /etc/opengrid/sim/scada.yaml --dsn ...` | service transformers (every hub mapped: each home bank's units sum to its kVA rating, 12 x 50 kVA for a 600 kVA bank (D-36), homes dealt round-robin; the substation set and each truck on its own transformer at its bank's kVA rating), feeder limits (the trucks' depot feeders included), substation limits, HOME_BANK asset rows. Uses fleet.yaml's enabled zone blocks, exactly as step 1; the substation set's rows from 2 are kept | 1, 2, 5 |

The firmware catalogue needs no seed: `[[firmware.catalogue]]` in `orchestrator/config/orchestrator.toml` (4 entries)
is merged with `og.firmware_catalogue` (0037, empty on a fresh database).

Seeds 2 to 4 are plain SQL: `psql -h 127.0.0.1 -U opengrid -d og -v ON_ERROR_STOP=1 -f dev/seed/<file>` with
`PGPASSWORD` from `/etc/opengrid/secrets.env` (never on the command line).

## After the services are up (optional)

- `dev/scripts/seed_demo_customers.py` (`bootstrap_from_scratch.sh --demo-customers`): offers the three 0002 demo
  contracts through og-api's admission for a one-hour window and waits for the selector to commit them. It needs a
  running og-api and `OG_API_PROXY_SECRET`.
- `orchestrator/tools/ercot_backfill.py --days 14` (phase k): 14 days of ERCOT prices and load into `og.feed_obs`,
  so the forecast is FIRM on a fresh database instead of the pooled rule. It needs the owner's ERCOT keys in
  `/etc/opengrid/api_keys.env` and is capped at 6 requests per minute.

## Existing databases: topology backfill

A database seeded before step 6 existed (or before it covered the substation set and the trucks) raises
ALR-XFMR-UNMAPPED for every bank the guardian checks. `deploy/scripts/topology_backfill.sh` runs step 6 with
`--only-missing`: insert-only, verified in one transaction (no existing og.bank/og.hub/limit/asset row changes),
dry run by default (`--apply` commits). `deploy/scripts/bootstrap_check.py` lists the unmapped counts.

## Not part of a fresh build

- `add_austin_fleet.sql`: an SQL-only way to add the LZ_AEN **and** LZ_CPS blocks. Production seeds LZ_AEN through
  step 1 instead (the same ids), and LZ_CPS is not enabled; do not run both.
- `rebalance_dual_units.sql`: a one-off backfill for databases seeded before the per-bank dual-unit rule. Step 1
  already applies that rule.

## Expected result

`make bootstrap-check` (phases c-e into a fresh `og_t_boot` on the test cluster, port 5433) prints every count; the
expected values on r3.3 + rm-r34 are listed in `deploy/BOOTSTRAP.md`, section 0.
