# Bootstrap a base server from scratch

This is the order that rebuilds a production node like `basepower` (192.168.5.35) from an empty Debian host.
Everything runs as root over SSH. Secrets are generated on the server and never printed, committed or
pasted into a chat. `deploy/README.md` and `deploy/RUNBOOK.md` cover day-to-day operation; this page covers
only the first build.

Time: about 45 minutes, most of it the Postgres initialisation and the first fleet seed.

To install on a new **Kubernetes** cluster instead, use `deploy/k8s/install.sh`; see
[k8s/README.md](k8s/README.md) (prerequisites, sizing, step by step, troubleshooting). It reuses this page's
scripts for the schema, migrations, seeds, sim configs, ACL, keys and checks.

## 0. Automated: `deploy/scripts/bootstrap_from_scratch.sh`

Steps 3 to 10 are automated by one idempotent script. Do step 1 (storage) and step 2 (packages and the two venvs)
by hand, place the owner-supplied files (below), then run it as root from the release checkout:

```bash
bash deploy/scripts/bootstrap_from_scratch.sh --dry-run          # what each phase would do; changes nothing
bash deploy/scripts/bootstrap_from_scratch.sh                    # phases a-l
bash deploy/scripts/bootstrap_from_scratch.sh --phase c-e        # only the database and its seeds
```

| Phase | Does | Step |
|---|---|---|
| a | checks packages, venvs, the release tree and the owner-supplied files (read-only) | 2 |
| b | the `opengrid` user and the directories (`/etc/opengrid`, `/var/lib/opengrid`, `/srv/ogbackup/{anchors,cold}`) | 1, 4 |
| c | `create_schema.sh --no-migrate`: the Postgres role, database, extensions and schema `og`; generates `OG_DB_PASSWORD` into `secrets.env` and sets it over stdin | 3 |
| d | `create_schema.sh`: every migration (`opengrid.platform.db migrate`); on `--fresh-db` also the snapshot comparison | 6 |
| e | all seeds in order (see `dev/seed/README.md`), then `deploy/scripts/bootstrap_check.py` asserts the counts | 7, 8 |
| f | `/etc/opengrid/sim/{fleet,scada}.yaml` with the approved zone blocks on (`deploy/scripts/gen_sim_overrides.py`) | 8 |
| g | the MQTT users (`og_engine`, `og_guardian`, `og_safestop`, `og_api`, `og_sim`, `og_simctl`, `og_sim_customer`): passwords generated into `secrets.env`/`customer_sim.env`, hashed with `mosquitto_passwd -U`; the ACL from `dev/scripts/gen_mosquitto_acl.py` plus production's two extra grants; `conf.d/opengrid.conf` | 4, 5 |
| h | the guardian, safestop and trace-anchor Ed25519 seeds (the orchestrator's own keygen CLIs) | 5 |
| i | the proxy secret (`api_proxy.env` and the Apache `Define`), then `install.sh` (Apache conf, htpasswd operator/viewer/tester/og-op-a/og-op-b, cron, logrotate, units), then the eight `og-cust-*` accounts with matching `customer_sim.env` entries | 4, 5 |
| j | unit files and timers from the release, the sim drop-ins (`og-sim-{fleet,scada}.service.d/austin.conf`), `opengrid.target`/`ogsim.target` enabled; `og-lifecycle.timer` stays disabled | 6, 9 |
| k | optional ERCOT backfill (`orchestrator/tools/ercot_backfill.py --days 14`), only when `api_keys.env` holds the ERCOT keys | 10 |
| l | the first release through `deploy.sh` (or a start when `/opt/opengrid/current` exists), then the checks of step 10 (`bootstrap_check.py --live`) | 6, 10 |

Options: `--zones LZ_AEN` (default, the production set), `--d32` (adds LZ_LCRA and LZ_RAYBN), `--demo-customers`
(phase l runs `dev/scripts/seed_demo_customers.py`), `--backfill-days N`, `--public-url URL` (the Apache-fronted URL
the customer simulator calls), `--etc DIR`, `--db-port`/`--db-name`/`--db-role` and `--fresh-db` (refused on 5432).
The script runs itself at idle I/O priority and nice 19.

**Every phase converges.** An existing secret, key, account, MQTT user or ACL is kept, never rotated and never
printed; generated passwords go only to the env files (640 root:opengrid, `api_proxy.env` 600 root) and
`/root/opengrid-ui-credentials.txt` (600). An existing ACL is not rewritten; a missing user block is reported.

**Owner-supplied inputs (not generated):**

| File | Needed for | Without it |
|---|---|---|
| `/etc/opengrid/api_keys.env` (`ERCOT_API_USER`, `ERCOT_API_PASSWORD`, `ERCOT_PUBLIC_API_KEY_*`, `ERCOT_STORAGE_API_KEY_*`, `EIA_API_KEY`; template `docs/orchestrator/07-delivery/integrations/api_keys.env.example`) | og-feeds live data and phase k | the units that load it do not start (`EnvironmentFile=` without `-`): create it, even empty, and point og-feeds at the market simulator |
| `/etc/opengrid/ai_agent.env` (`ANTHROPIC_API_KEY`) | the AI copilot's model tier | optional; the copilot runs its no-model tier |

**Check on the test cluster:** `make bootstrap-check` runs phases c-e into a fresh `og_t_boot` on port 5433 (role
`og_boot`, secrets under `/srv/ogwork/bootstrap/etc`), never on 5432. Expected on r3.4.1 with `--d32` (production's
blocks: LZ_AEN, LZ_LCRA, LZ_RAYBN): 43/43 migrations; 3,500 home hubs (500 in each of LZ_NORTH, LZ_SOUTH,
LZ_HOUSTON, LZ_WEST, LZ_AEN, LZ_LCRA, LZ_RAYBN; 700 dual-unit) plus the substation hub and the 8 trucks (3,509
`og.hub` rows); 70 home banks plus `bank-sub-LZ_AEN-00` and the 8 truck banks (79); substation asset
`sub-LZ_AEN-00` ACTIVE; utilities AUSTIN_ENERGY ($102/kW-yr) and CPS_ENERGY; the toll contract
(REGULATED_CAPACITY/TOLLING); 11 contracts (the 8 customer contracts, the toll and the other migration demo rows); 849
service transformers (12 x 50 kVA per home bank, D-36; one each for the substation set and the 8 trucks), every hub mapped;
23 feeder limits; 7 substation limits; 79 assets (70 HOME_BANK, 1 SUBSTATION, 8 MOBILE_STORAGE); every
`opengrid.fleet.topology_audit` unmapped count 0 (no ALR-XFMR-UNMAPPED / ALR-BANK-UNMAPPED-TOPOLOGY source); the
FLEET charge window `22:00-06:00`; 4 firmware catalogue entries from config. A database seeded before r3.4.1 gets
the missing topology rows from `deploy/scripts/topology_backfill.sh` (dry run by default, insert-only).

### Database schema: `deploy/scripts/create_schema.sh` and `orchestrator/schema/og_schema.sql`

`create_schema.sh` builds the database from nothing and is what phases c and d run:

1. It generates the role's password into `secrets.env` when absent and sets it over stdin (never in argv), then
   converges it on every run.
2. It creates the role (LOGIN) and the database `og` (owned by that role).
3. It creates the extensions: none are required. `gen_random_uuid()` is core since PostgreSQL 13, and the script
   refuses an older server. A future extension goes in its `EXTENSIONS` list, never in a migration, because
   creating one needs a superuser.
4. It creates schema `og`, owned by the role.
5. It applies migrations 0001..latest through the one runner (`python -m opengrid.platform.db migrate`) and checks
   that every file is recorded in `og.schema_migrations`.

It is idempotent: an existing role, database or schema is kept, and only pending migrations run. Options are
`--db-port`/`--db-name`/`--db-role`/`--etc`, `--fresh-db` (refused on 5432), `--no-migrate` and `--dry-run`.

`orchestrator/schema/og_schema.sql` is a generated, read-only snapshot of the consolidated schema. It is
`pg_dump --schema-only --no-owner --no-privileges` of schema `og` on a fresh database after every migration. It is
for review and diffing only; a new database is always built by the migrations, never from the snapshot.

- `make schema-check` rebuilds a fresh `og_t_schema` on 5433 and fails (exit 3) when the migrations no longer
  produce exactly the committed file.
- `make schema-snapshot` regenerates the file. Commit it together with the migration that changed it.
- On a `--fresh-db` bootstrap, phase d runs the same comparison and prints a warning on drift.

The committed snapshot is at the r3.4 migration set (0001-0043, 42 files; 0015 does not exist). It was generated on
the assembled r3.4 tree (integ/bootstrap + fix-h4-devinfo + fix-ui-r34), so `make schema-check` passes only once
0041-0043 are in the tree.

## 1. Storage

Create the logical volumes and mount them (ext4, `noatime`):

| Mount | Size (today) | Holds |
|---|---|---|
| `/srv/pgdata` | 400 G | the production Postgres cluster (17/main) |
| `/srv/pgstandby` | 100 G | the disposable test cluster (17/ogtest) |
| `/srv/ogbackup` | 60 G | `pg_dump` backups, cold-tier exports (`cold/`), trace anchors (`anchors/`) |

On a single slow disk, production must win I/O contention: every og-* unit carries `IOWeight=500`, and the
lifecycle job and test runs run at idle I/O priority.

## 2. Packages

```bash
apt-get install postgresql-17 mosquitto apache2 python3.13-venv sysstat
```

Python packages are installed only by the live-path role, from downloaded wheels, and are logged in
`docs/team/NOTICES.md` (policy there). `deploy/scripts/install.sh` creates both venvs:
`/opt/opengrid/venv` (orchestrator, with `pip install -e orchestrator`) and `/opt/ogsim/venv` (simulators).

## 3. Postgres

1. Production cluster `17/main`, with its data directory on `/srv/pgdata/17/main` (`data_directory` in
   `postgresql.conf`). Settings via `ALTER SYSTEM`:
   `shared_buffers = '2GB'`, `max_wal_size = '4GB'`, `checkpoint_timeout = '15min'`,
   `wal_compression = 'zstd'`, `commit_delay = 1000`, `commit_siblings = 5`.
2. Role `opengrid` (LOGIN, owner of database `og`). Generate its password on the server into
   `/etc/opengrid/secrets.env` as `OG_DB_PASSWORD=` (see step 5), then
   `ALTER ROLE opengrid PASSWORD ...` from that file without echoing it.
3. Test cluster `17/ogtest` on port 5433, data in `/srv/pgstandby/17/ogtest`, with `fsync = off`,
   `synchronous_commit = off`, `full_page_writes = off`, `wal_level = minimal` (test data is disposable).
   Copy the production role's SCRAM verifier into it (`SELECT rolpassword FROM pg_authid ...` on 5432, then
   `ALTER ROLE opengrid PASSWORD '<verifier>'` on 5433) so workspaces use the same env file.
   `tools/ws_env.sh` points every workspace at 5433 (`OG_DB_PORT`/`PGPORT`); never create `og_t_*`
   databases on 5432.

## 4. Install

```bash
bash deploy/scripts/install.sh <release_dir>
```

It creates the `opengrid` user and `/etc/opengrid` (750 root:opengrid), installs the systemd units (not
enabled), the Apache conf fragment (`a2enconf opengrid`), log rotation, the backup cron, the Mosquitto users
and ACL, and the UI accounts (operator, viewer, tester, the two-person release operators and the eight
customer accounts). Generated passwords go only into `/root/opengrid-ui-credentials.txt` (600).

As of r3.3, `install.sh` itself does the units, the Apache conf, the htpasswd accounts operator/viewer/tester and the
two release operators, cron and logrotate. The `opengrid` user, `/etc/opengrid`, the Mosquitto users and ACL, the
customer accounts and the proxy secret are done by `bootstrap_from_scratch.sh` phases b, g and i (section 0).

## 5. Secrets and keys (generated on the server, never printed)

| File | Mode | Content |
|---|---|---|
| `/etc/opengrid/secrets.env` | 640 root:opengrid | `OG_DB_PASSWORD`, the MQTT role passwords |
| `/etc/opengrid/api_keys.env` | 640 root:opengrid | ERCOT/EIA keys and ERCOT API user |
| `/etc/opengrid/api_proxy.env` | 640 root:opengrid | the Apache-to-API proxy secret |
| `/etc/opengrid/ai_agent.env` | 640 root:opengrid | the owner's Anthropic key (installed by the owner) |
| `/etc/opengrid/customer_sim.env` | 640 root:opengrid | customer-simulator MQTT user and account names |
| `/etc/opengrid/guardian_ed25519.key` | 600 opengrid | raw 32-byte Ed25519 seed |
| `/etc/opengrid/safestop_ed25519.key` | 600 opengrid | raw 32-byte Ed25519 seed |
| `/etc/opengrid/trace_anchor_ed25519.key` | 600 opengrid | raw 32-byte Ed25519 seed (K11 anchors) |

A seed is 32 random bytes:
`/opt/opengrid/venv/bin/python -c "import os,sys; sys.stdout.buffer.write(os.urandom(32))" > <file>`, then
`chown opengrid:opengrid <file>; chmod 600 <file>`. Check a file only by counting (`grep -c`), never by
printing it.

## 6. First release

```bash
git -C /opt/opengrid/src archive <tag> | tar -x -C /root/release-<tag>
bash /opt/opengrid/deploy/scripts/deploy.sh /root/release-<tag>
```

`deploy.sh` applies every migration, seeds the base fleet, installs the release's unit files, switches
`/opt/opengrid/current`, restarts `opengrid.target` and `ogsim.target` and health-checks. On a first
build, enable the targets once: `systemctl enable opengrid.target ogsim.target`.

## 7. Seeds (after the first deploy)

Phase e runs the complete list in order (fleet with the zone blocks, market model, customer services, services,
trucks, topology); `dev/seed/README.md` describes each seed. By hand, the three SQL seeds are:

As `opengrid`, with `PGPASSWORD` from `secrets.env`:

```bash
psql -h 127.0.0.1 -U opengrid -d og -v ON_ERROR_STOP=1 -f dev/seed/customer_services_seed.sql
psql ... -f dev/seed/services_seed.sql
psql ... -f dev/seed/market_model_seed.sql     # utilities, the Austin toll contract, the substation asset
```

## 8. Enabled zone blocks

The repo's `integration-sims/config/{fleet,scada}.yaml` declare every zone block. Production enables the ones
the owner approved through generated copies in `/etc/opengrid/sim/` plus drop-ins that point
`og-sim-fleet`/`og-sim-scada` at them (`deploy/README.md`, "Austin (LZ_AEN) sim override"). Seed those hubs
with `OG_FLEET_SIM_CONFIG=/etc/opengrid/sim/fleet.yaml python -m opengrid.fleet.seed`, then restart both
targets. Regenerate the copies on every release that changes either yaml.

## 9. Data lifecycle

`deploy.sh` installs `og-lifecycle.service`/`.timer` but never enables the timer. Enable it only after an I/O
check on `/srv/pgdata` (`iostat -dx 5`: util well below 80 % under normal load):
`systemctl enable --now og-lifecycle.timer`. Telemetry retention is 7 days (`og.data_retention`).

## 10. Verify

- `systemctl is-active` for all 10 og-* units; `GET /og/api/health` returns 200.
- `og.schema_migrations` holds every file in `orchestrator/migrations/`.
- Every hub is fresh within `health.hub_stale_s` (25 s at the 10 s telemetry cadence).
- All 12 invariants read 0 (`og.invariant_check`); a signed anchor appears in `/var/lib/opengrid/anchors`.
- `/og/` returns 401 without credentials and 200 for operator and viewer; `/ogsim/` returns 200 for tester.
- No degraded mode in `og.degraded_mode_state` once the ERCOT price feed and forecast are fresh. A fresh
  database needs about two weeks of price history for strict firm forecasts; until then the pooled rule
  applies, or backfill with `orchestrator/tools/ercot_backfill.py` (at most 6 requests per minute).
