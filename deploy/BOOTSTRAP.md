# Bootstrap a base server from scratch

This is the order that rebuilds a production node like `basepower` (192.168.5.35) from an empty Debian host.
Everything runs as root over SSH. Secrets are generated on the server and never printed, committed or
pasted into a chat. [README.md](README.md) and [RUNBOOK.md](RUNBOOK.md) cover day-to-day operation; this page covers
only the first build. It describes `deploy/scripts/bootstrap_from_scratch.sh` as of r3.4.3.

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
bash deploy/scripts/bootstrap_from_scratch.sh --phase x          # only bootstrap_check.py against the database
```

The script re-executes itself at idle I/O priority and nice 19 (`ionice -c3 nice -n 19`, `TMPDIR=/dev/shm`).
Phases run in this fixed order, whatever order `--phase` lists them in: **a, b, c, d, f, e, g, h, i, j, k, l**
(f before e, so the seeds read fresh sim configs), then x when selected.

| Phase | Does | Step |
|---|---|---|
| a | read-only prerequisites: the commands `psql openssl python3 runuser systemctl` (missing = stop), `mosquitto_passwd htpasswd apache2ctl` (warning), the orchestrator venv `/opt/opengrid/venv` (stop) and the simulator venv `/opt/ogsim/venv` (warning), the release tree (`orchestrator/migrations`, `orchestrator/src`, `integration-sims/config`, `deploy/systemd`, `dev/seed`), root; prints the release, the migration count and latest file, the target and the zone blocks, and whether the owner-supplied files are present | 2 |
| b | the `opengrid` system user; `<etc>` and `<etc>/sim` (750 root:opengrid); with the default `--etc` also `/opt/opengrid{,/releases}`, `/var/lib/opengrid/{anchors,backups,ercot_backfill}`, `/var/log/opengrid` and `/srv/ogbackup/{anchors,cold}` | 1, 4 |
| c | `create_schema.sh --no-migrate`: `OG_DB_PASSWORD` generated into `secrets.env` when absent, the role (LOGIN) and its password set over stdin, the database, the extensions (none) and schema `og`; with `--fresh-db` the database is dropped first | 3 |
| d | `create_schema.sh`: every migration through `python -m opengrid.platform.db migrate`, then a check that every file is recorded in `og.schema_migrations`; with `--fresh-db` (and a committed snapshot) also `--check-snapshot`, where drift (exit 3) is a warning and seeding continues | 6 |
| f | `deploy/scripts/gen_sim_overrides.py`: `<etc>/sim/{fleet,scada}.yaml` from the release's `integration-sims/config/`, with exactly the `--zones` blocks enabled (640 root:opengrid) | 8 |
| e | reruns phase f first when f was not selected, then the seeds in order: 1 fleet (`opengrid.fleet.seed` with `OG_FLEET_SIM_CONFIG=<etc>/sim/fleet.yaml`), 2 `market_model_seed.sql` (utilities, the $102/kW-yr Austin toll, substation asset `sub-LZ_AEN-00`), 2b `noie_switch_seed.sql` (D-37: LCRA/RAYBURN sample tolls, LZ_LCRA/LZ_RAYBN banks UNAVAILABLE), 3 `customer_services_seed.sql`, 4 `services_seed.sql`, 5 `mobile_trucks_seed.sql` (skipped when the release lacks it), 6 `topology_seed.py` (last: it maps every hub already in `og.hub`); then `bootstrap_check.py` (with `--expect-trucks` when the trucks seed ran) | 7, 8 |
| g | MQTT: `OG_MQTT_{ENGINE,GUARDIAN,SIM,API,SAFESTOP,SIMCTL}_PASSWORD` in `secrets.env` and `OG_MQTT_CUSTOMER_PASSWORD` in `customer_sim.env` (generated when absent); users `og_engine`, `og_guardian`, `og_sim`, `og_api`, `og_safestop`, `og_simctl`, `og_sim_customer` hashed into `/etc/mosquitto/opengrid.passwd` with `mosquitto_passwd -U`; the ACL from `deploy/scripts/render_mosquitto_acl.sh` (`dev/scripts/gen_mosquitto_acl.py` plus production's two extra grants) only when absent; `conf.d/opengrid.conf` (loopback listener 1883, no anonymous access) only when absent; Mosquitto restarted only when something changed | 4, 5 |
| h | the guardian, safestop and trace-anchor Ed25519 seeds (the orchestrator's own keygen CLIs) when absent, then the per-key groups and drop-ins (section 5) | 5 |
| i | `OG_API_PROXY_SECRET` in `api_proxy.env` (600 root:root) and the Apache `Define` in `/etc/apache2/conf-available/opengrid-proxy-secret.conf` (written through a process substitution, never argv); `install.sh` (section 4); one Apache account per `[api.roles.customer]` entry (today the eight `og-cust-*`), each with `OGSIM_CUSTOMER_<GROUP>_USER/_PASSWORD` in `customer_sim.env`; `OGSIM_CUSTOMER_API_BASE` = `--public-url` when unset; Apache reloaded after a passing configtest | 4, 5 |
| j | every `deploy/systemd/*.service`, `*.target` and `*.timer` into `/etc/systemd/system`, the sim drop-ins `og-sim-{fleet,scada}.service.d/austin.conf` (`OGSIM_{FLEET,SCADA}_CONFIG=<etc>/sim/*.yaml`), `opengrid.target` and `ogsim.target` enabled; `og-lifecycle.timer` stays disabled | 6, 9 |
| k | optional ERCOT backfill (`orchestrator/tools/ercot_backfill.py --days N`, at most 6 requests per minute), only when `api_keys.env` holds `ERCOT_PUBLIC_API_KEY_PRIMARY`; otherwise skipped with a note | 10 |
| l | `deploy.sh` for the first release (or `systemctl start opengrid.target ogsim.target` when `/opt/opengrid/current` exists); waits up to 60 s for `/og/api/health` 200; prints the state of the six og-* units and four sims, and `/og/` without credentials (expect 401); waits 45 s, then `bootstrap_check.py --live`; with `--demo-customers` also `dev/scripts/seed_demo_customers.py` | 6, 10 |
| x | only `bootstrap_check.py` against the target database (not part of the default a-l) | - |

Options (every one the script accepts):

| Option | Default | Effect |
|---|---|---|
| `--phase LIST` | `a-l` | letters and ranges, e.g. `c-e` or `g,h,i` |
| `--dry-run` | off | print what each phase would do; change nothing |
| `--release DIR` | the script's own repo | release/repo root |
| `--etc DIR` | `/etc/opengrid` | secrets and config directory (the sim configs go to `<etc>/sim`) |
| `--db-port N`, `--db-name NAME`, `--db-role ROLE` | `5432`, `og`, `opengrid` | target database |
| `--db-host HOST` | localhost | database host of the DB phases |
| `--config FILE` | the release's `orchestrator/config/orchestrator.toml` | config of the DB phases; its `[postgres].host` must be `--db-host` (deploy/k8s renders one per pod) |
| `--fresh-db` | off | drop the database first; refused on port 5432 |
| `--zones LIST` | `LZ_AEN` | zone blocks to enable (the owner-approved production set) |
| `--noie-blocks` | off | also enable LZ_LCRA and LZ_RAYBN: regulated (NOIE), UNAVAILABLE, no contract (D-37) |
| `--d32` | - | deprecated alias of `--noie-blocks` (D-32 is superseded by D-37) |
| `--demo-customers` | off | phase l: run `dev/scripts/seed_demo_customers.py` once the API is up |
| `--backfill-days N` | `14` | phase k window |
| `--public-url URL` | `https://<fqdn>` | Apache-fronted URL the customer simulator calls |
| `--migrate-key-perms` | off | phase h: move keys still in the legacy `opengrid:opengrid 0600` layout to the per-key groups (section 5) |

Environment used by the DB phases (exported only inside a subshell, password from `secrets.env`): `OG_DB_PASSWORD`,
`PGPASSWORD`, `OG_CONFIG`, `PYTHONPATH`, `OG_DB`, `OG_DB_PORT`, `OG_DB_USER`, `PGHOST`, `PGPORT`, `PGUSER`,
`PGDATABASE` and `OG_FLEET_SIM_CONFIG=<etc>/sim/fleet.yaml`. `OG_BOOT_THROTTLED` marks the throttled re-exec.

**Every phase converges.** An existing secret, key, account, MQTT user or ACL is kept, never rotated and never
printed; generated secrets (`openssl rand -hex 24`, through `deploy/scripts/lib_secrets.sh`) go only to the env
files (640 root:opengrid; `api_proxy.env` 600 root:root) and `/root/opengrid-ui-credentials.txt` (600). An existing
ACL is not rewritten; a missing user block is reported as a warning.

**Not done by the script (r3.4.3):** the utility-side accounts and units that shipped with r3.4.1-r3.4.3 are
installed by their own release-manager scripts, each a dry run unless `APPLY=1` (see [RUNBOOK.md](RUNBOOK.md),
"Utility API, utility simulator and grid link"): the Austin Energy Apache account `og-util-aen` and
`/etc/opengrid/utility_sim.env` (`r341_utility_api_apache.sh`), the MQTT user `og_sim_utility` and
`og-sim-utility.service` (`r343_utility_sim_install.sh`), and the grid link's `og_gridlink` user and test PKI
(`grid_link_enable_loopback.sh`; the link stays off by default). Note: `ogsim.target` now wants
`og-sim-utility`, which needs `/etc/opengrid/utility_sim.env` (its `EnvironmentFile=` has no `-`), and
`deploy.sh` requires every og-* unit of both targets to be active. On a new host, run
`APPLY=1 bash deploy/scripts/r341_utility_api_apache.sh` after phase i and
`APPLY=1 SRC=<release>/deploy/systemd bash deploy/scripts/r343_utility_sim_install.sh` before phase l, or the
first `deploy.sh` fails its post-restart check.

**Owner-supplied inputs (not generated):**

| File | Needed for | Without it |
|---|---|---|
| `/etc/opengrid/api_keys.env` (`ERCOT_API_USER`, `ERCOT_API_PASSWORD`, `ERCOT_PUBLIC_API_KEY_*`, `ERCOT_STORAGE_API_KEY_*`, `EIA_API_KEY`; template `docs/orchestrator/07-delivery/integrations/api_keys.env.example`) | og-feeds live data and phase k | the units that load it do not start (`EnvironmentFile=` without `-`) and `deploy.sh` sources it: create it, even empty, and point og-feeds at the market simulator |
| `/etc/opengrid/ai_agent.env` (`ANTHROPIC_API_KEY`) | the AI copilot's model tier | optional; the copilot runs its no-model tier |

**Check on the test cluster:** `make bootstrap-check` runs phases c-e with `--fresh-db` into `og_t_boot` on port
5433 (role `og_boot`, secrets under `/srv/ogwork/bootstrap/etc`), never on 5432, with the default zone blocks
(LZ_AEN only). The counts below are for production's blocks (`--noie-blocks`: LZ_AEN, LZ_LCRA, LZ_RAYBN), as last
measured on r3.4.2; r3.4.3 adds migrations 0050 (a table, no seed rows), 0051 (one `og.data_retention` row,
`delivery_record`, mode NONE, plus a column), 0053 (`og.verdict.published_at`) and 0054 (the `og.trace`
decision-type CHECK accepts `DELIVERY_RECORD`). Every migration through 0054 applied (0015, 0048 and 0049 do not
exist; 51 files with 0051 to 0054); 3,500 home hubs (500 in each of LZ_NORTH, LZ_SOUTH,
LZ_HOUSTON, LZ_WEST, LZ_AEN, LZ_LCRA, LZ_RAYBN; 700 dual-unit) plus the substation hub and the 8 trucks (3,509
`og.hub` rows); 70 home banks plus `bank-sub-LZ_AEN-00` and the 8 truck banks (79); substation asset
`sub-LZ_AEN-00` ACTIVE; utilities AUSTIN_ENERGY ($102/kW-yr), CPS_ENERGY, and (D-37) LCRA and RAYBURN (placeholder
terms); the toll contract (REGULATED_CAPACITY/TOLLING); 13 contracts (the 8 customer contracts, the toll, the other
migration demo rows, and the 2 inactive `Sample Contract: ...` tolls: `is_sample`, SUSPENDED, 6,000 kW / 90 min); D-37
availability: the 20 LZ_LCRA/LZ_RAYBN banks `UNAVAILABLE` / `REGULATED_NO_CONTRACT` ("Regulated market – no
contract"), every other bank `AVAILABLE`, and their 20 HOME_BANK assets carry utility LCRA/RAYBURN; 849
service transformers (12 x 50 kVA per home bank, D-36; one each for the substation set and the 8 trucks), every hub mapped;
23 feeder limits; 7 substation limits; 79 assets (70 HOME_BANK, 1 SUBSTATION, 8 MOBILE_STORAGE); every
`opengrid.fleet.topology_audit` unmapped count 0 (no ALR-XFMR-UNMAPPED / ALR-BANK-UNMAPPED-TOPOLOGY source); the
FLEET charge window `22:00-06:00`; 4 firmware catalogue entries from config. A database seeded before r3.4.1 gets
the missing topology rows from `deploy/scripts/topology_backfill.sh` (dry run by default, insert-only; see
[RUNBOOK.md](RUNBOOK.md), "Topology backfill").

**What `deploy/scripts/bootstrap_check.py` asserts** (read-only; exit 0 = PASS, 1 = FAIL; counts only, never a
credential). Expected values are derived, not hard-coded twice: the fleet from `opengrid.fleet.seed.build_topology`
over the same `OG_FLEET_SIM_CONFIG` the seed used, the migrations from `orchestrator/migrations/`, the firmware
catalogue from `[firmware.catalogue]`.

- Migrations: every file recorded in `og.schema_migrations` (prints `applied/files (latest ...)`).
- Fleet: home hubs per zone and in total, home banks, dual-unit hubs (the 20 % rule); the substation asset ACTIVE
  and its bank and hub; AUSTIN_ENERGY and CPS_ENERGY; the toll contract REGULATED_CAPACITY/TOLLING; AUSTIN_ENERGY at
  $102/kW-yr; the 8 seeded customer contracts; the contract total (info).
- D-37 (skipped with a note when migration 0046 is absent): the utilities are exactly AUSTIN_ENERGY, CPS_ENERGY,
  LCRA and RAYBURN; both sample contracts are `is_sample`, SUSPENDED and named `Sample Contract...`, with no
  opportunity or obligation; the LZ_LCRA/LZ_RAYBN banks are `UNAVAILABLE`/`REGULATED_NO_CONTRACT` (0 expected
  without `--noie-blocks`); no bank outside those zones is anything but `AVAILABLE`; their HOME_BANK assets belong
  to LCRA/RAYBURN; `tdsp_tariffs.toml` `[zone_territory]` maps LZ_AEN, LZ_CPS, LZ_LCRA and LZ_RAYBN to their
  utility, REGULATED.
- Topology: every `topology_audit` unmapped count 0, at least one feeder limit per feeder in use, the transformer,
  substation-limit and asset totals (info).
- The FLEET `*` charge window `22:00-06:00`; at least one `[firmware.catalogue]` entry and `og.firmware_catalogue`
  present; trucks as `og.asset` MOBILE_STORAGE rows (required with `--expect-trucks`).
- `--live` adds: every hub fresh within `--stale-s` (default 25 s), invariant violations 0, and the degraded modes
  (info; a fresh database without backfill shows the forecast modes).

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
`--release`, `--etc`, `--db-port`/`--db-name`/`--db-role`, `--db-host`, `--config`, `--fresh-db` (refused on
5432), `--no-migrate`, `--snapshot FILE`, `--check-snapshot` and `--dry-run`. The admin connection is
`runuser -u postgres` on the local socket; when `OG_PG_ADMIN_PASSWORD` is set in the environment (never argv) it
connects over TCP to `--db-host` as `OG_PG_ADMIN_USER` (default `postgres`) instead, as the deploy/k8s migrations
Job does. `PG_BIN` (default `/usr/lib/postgresql/17/bin`) selects `pg_dump`.

`orchestrator/schema/og_schema.sql` is a generated, read-only snapshot of the consolidated schema:
`pg_dump --schema-only --no-owner --no-privileges --no-comments --schema=og` on a fresh database after every
migration, with the psql `\restrict` lines and version banners removed. It is for review and diffing only; a new
database is always built by the migrations, never from the snapshot.

- `make schema-check` (`create_schema.sh --fresh-db --check-snapshot`) rebuilds a fresh `og_t_schema` on 5433 and
  fails (exit 3; full diff in `/dev/shm/og_schema.diff`) when the migrations no longer produce exactly the
  committed file.
- `make schema-snapshot` (`--snapshot orchestrator/schema/og_schema.sql`) regenerates the file. Commit it together
  with the migration that changed it.
- On a `--fresh-db` bootstrap, phase d runs the same comparison and prints a warning on drift.

In r3.4.3 the committed snapshot was regenerated through 0050 during the release assembly. The migrations merged
after it (0051 `delivery_record.series_pruned_at` and its index, 0053 `og.verdict.published_at`, 0054 the widened
`og.trace` CHECK) change the schema, so unless the snapshot was regenerated again, `make schema-check` reports
drift and a `--fresh-db` bootstrap prints the phase d warning and continues.

## 1. Storage

Create the logical volumes and mount them (ext4, `noatime`):

| Mount | Size (today) | Holds |
|---|---|---|
| `/srv/pgdata` | 400 G | the production Postgres cluster (17/main) |
| `/srv/pgstandby` | 100 G | the disposable test cluster (17/ogtest) |
| `/srv/ogbackup` | 60 G | `pg_dump` backups, cold-tier exports (`cold/`), trace anchors (`anchors/`) |

On a single slow disk, production must win I/O contention: the six orchestrator units carry `IOWeight=500`, while
the lifecycle job (`IOSchedulingClass=idle`, `IOWeight=10`), `og-sim-utility` (`IOWeight=10`) and test runs yield.

## 2. Packages

```bash
apt-get install postgresql-17 mosquitto apache2 python3.13-venv sysstat
```

Python packages are installed only by the live-path role, from downloaded wheels, and are logged in
`docs/team/NOTICES.md` (policy there). The two venvs are created by hand before the bootstrap:
`/opt/opengrid/venv` (orchestrator, with `pip install -e orchestrator`) and `/opt/ogsim/venv` (simulators).
Neither `install.sh` nor `bootstrap_from_scratch.sh` creates them; phase a only checks them.

## 3. Postgres

1. Production cluster `17/main`, with its data directory on `/srv/pgdata/17/main` (`data_directory` in
   `postgresql.conf`). Settings via `ALTER SYSTEM`:
   `shared_buffers = '2GB'`, `max_wal_size = '4GB'`, `checkpoint_timeout = '15min'`,
   `wal_compression = 'zstd'`, `commit_delay = 1000`, `commit_siblings = 5`.
2. Role `opengrid` (LOGIN, owner of database `og`). Phase c (`create_schema.sh`) generates its password on the
   server into `/etc/opengrid/secrets.env` as `OG_DB_PASSWORD=` and sets it over stdin without echoing it.
3. Test cluster `17/ogtest` on port 5433, data in `/srv/pgstandby/17/ogtest`, with `fsync = off`,
   `synchronous_commit = off`, `full_page_writes = off`, `wal_level = minimal` (test data is disposable).
   Copy the production role's SCRAM verifier into it (`SELECT rolpassword FROM pg_authid ...` on 5432, then
   `ALTER ROLE opengrid PASSWORD '<verifier>'` on 5433) so workspaces use the same env file.
   `tools/ws_env.sh` points every workspace at 5433 (`OG_DB_PORT`/`PGPORT`); never create `og_t_*`
   databases on 5432.

## 4. Install

```bash
bash deploy/scripts/install.sh <release_dir>/deploy
```

`install.sh` takes the release's `deploy/` directory and needs the `opengrid` user and `/etc/opengrid` to exist
(phase b). As of r3.4.3 it does, idempotently:

- the systemd units (`*.service`, `*.target`; installed, not enabled or started; timers are left to phase j and
  `deploy.sh`);
- the stable script path `/opt/opengrid/deploy/scripts` with `deploy.sh`, `rollback.sh` and `backup.sh` (750
  root:opengrid), `/opt/opengrid/releases` and `/var/lib/opengrid/backups`;
- search access for `www-data` into `/etc/opengrid` (`setfacl`, else `o+x`);
- the Apache modules `proxy proxy_http headers auth_basic` and `conf-available/opengrid.conf` (`a2enconf
  opengrid`); on a failed `apache2ctl configtest` the previous conf is restored and Apache is not reloaded;
- `/etc/opengrid/htpasswd` with `operator`, `viewer`, `tester` only when the file does not exist yet, then one
  account per `[guardian].stop_release_authorised_operators` entry (today `og-op-a`, `og-op-b`) when missing;
  generated passwords go only to `/root/opengrid-ui-credentials.txt` (600);
- the backup cron (`/etc/cron.d/opengrid`) and log rotation (`/etc/logrotate.d/opengrid`).

It never touches `/etc/mosquitto`. The `opengrid` user, `/etc/opengrid`, the Mosquitto users and ACL, the customer
accounts and the proxy secret are done by `bootstrap_from_scratch.sh` phases b, g and i (section 0).

## 5. Secrets and keys (generated on the server, never printed)

| File | Mode | Content | Written by |
|---|---|---|---|
| `/etc/opengrid/secrets.env` | 640 root:opengrid | `OG_DB_PASSWORD`, the MQTT role passwords (`OG_MQTT_GRIDLINK_PASSWORD` once the grid link is enabled) | phases c, g; `grid_link_enable_loopback.sh` |
| `/etc/opengrid/api_keys.env` | 640 root:opengrid | ERCOT/EIA keys and ERCOT API user | the owner |
| `/etc/opengrid/api_proxy.env` | 600 root:root | the Apache-to-API proxy secret | phase i |
| `/etc/apache2/conf-available/opengrid-proxy-secret.conf` | umask 077 | the same secret as an Apache `Define` | phase i |
| `/etc/opengrid/ai_agent.env` | 640 root:opengrid | the owner's Anthropic key | the owner |
| `/etc/opengrid/customer_sim.env` | 640 root:opengrid | customer-simulator MQTT password, account names and passwords, API base | phases g, i |
| `/etc/opengrid/utility_sim.env` | 640 root:opengrid | utility-simulator API base, `og-util-aen` account, `OG_MQTT_UTILITY_PASSWORD` | `r341_utility_api_apache.sh`, `r343_utility_sim_install.sh` |
| `/etc/opengrid/htpasswd` | 640 root:www-data | Apache accounts (bcrypt) | `install.sh`, phase i, `r341_utility_api_apache.sh` |
| `/etc/opengrid/grid_link.toml`, `/etc/opengrid/certs/` | 640 root:opengrid; dir 750; CA key 600 root:root | grid-link override and loopback test PKI | `grid_link_enable_loopback.sh` |
| `/etc/opengrid/guardian_ed25519.key` | 640 root:og-guardian-key | raw 32-byte Ed25519 seed; read by og-guardian only | phase h |
| `/etc/opengrid/safestop_ed25519.key` | 640 root:og-safestop-key | raw 32-byte Ed25519 seed; read by og-safestop only | phase h |
| `/etc/opengrid/trace_anchor_ed25519.key` | 640 root:og-anchor-key | raw 32-byte Ed25519 seed (K11 anchors); read by og-settle only | phase h |
| `/root/opengrid-ui-credentials.txt`, `/root/opengrid-utility-credentials.txt` | 600 root | generated account passwords for hand-over | `install.sh`, phase i; `r341_utility_api_apache.sh` |

Phase h generates the seeds with the orchestrator's CLIs (`python -m opengrid.guardian keygen` for the guardian
and trace-anchor keys, `python -m opengrid.safestop.keys keygen` for the safestop key), each with its `.pub`.
By hand, a seed is 32 random bytes:
`/opt/opengrid/venv/bin/python -c "import os,sys; sys.stdout.buffer.write(os.urandom(32))" > <file>`, then
`chown root:<key group> <file>; chmod 640 <file>`. Check a file only by counting (`grep -c`), never by printing it.

**Key access.** Every og-* unit and every simulator runs as `opengrid`, so a key owned by `opengrid` is
readable by og-api and the simulators too. Each private seed therefore belongs to its own group
(`og-guardian-key`, `og-safestop-key`, `og-anchor-key`; system groups, and the `opengrid` user is a member of
none), and only its signing unit gets that group through a drop-in, `/etc/systemd/system/<unit>.service.d/keys.conf`
with `SupplementaryGroups=<key group>` (og-guardian, og-safestop, og-settle). Phase h creates the groups, writes
the three drop-ins, sets each key `root:<group> 0640` and each `.pub` `root:root 0644`, and reloads systemd. The
safe-stop CLI runs as root and is unaffected. (The script's header comment still says "keys 600 opengrid"; the
code does the per-group layout above.)

A key still in the legacy layout (`opengrid:opengrid 0600`) is left untouched, with a note, unless
`--migrate-key-perms` is given: the running service needs the drop-in and a restart at the same moment.

*Production (checked 2026-09-26, read-only):* the three keys were `opengrid:opengrid 0600` and no unit had a
supplementary group. **Migration step (for the release manager):** only outside any FIRM/AS delivery window
(`deploy.sh`'s DELIVERING preflight query must return no rows), run `bash deploy/scripts/bootstrap_from_scratch.sh
--phase h --migrate-key-perms` from the release, then restart og-guardian, og-safestop and og-settle one at a time
and check each is active. Folding `SupplementaryGroups=` into the three unit files would make the drop-ins
unnecessary.

## 6. First release

```bash
git -C /opt/opengrid/src archive <tag> | tar -x -C /root/release-<tag>
bash /opt/opengrid/deploy/scripts/deploy.sh /root/release-<tag>
```

`deploy.sh` (details in [RUNBOOK.md](RUNBOOK.md), "Deploy a release") copies the release to
`/opt/opengrid/releases/<timestamp>`, applies every migration, re-runs the fleet seed (from
`/etc/opengrid/sim/fleet.yaml` when present), switches `/opt/opengrid/current`, installs the release's unit files
and timers (never enables a timer), creates the runtime directories, restarts `opengrid.target` and `ogsim.target`,
and checks `/og/api/health`, every og-* unit of both targets and a fresh heartbeat from each orchestrator process.
Phase j already enabled the targets; by hand: `systemctl enable opengrid.target ogsim.target`.

## 7. Seeds (after the first deploy)

Phase e runs the complete list in order (fleet with the zone blocks, market model, D-37 NOIE switch, customer
services, services, trucks, topology); [dev/seed/README.md](../dev/seed/README.md) describes each seed. By hand,
the SQL seeds are:

As `opengrid`, with `PGPASSWORD` from `secrets.env`:

```bash
psql -h localhost -U opengrid -d og -v ON_ERROR_STOP=1 -f dev/seed/market_model_seed.sql   # utilities, the Austin toll contract, the substation asset
psql ... -f dev/seed/noie_switch_seed.sql        # D-37: LCRA/RAYBURN sample tolls, NOIE banks UNAVAILABLE
psql ... -f dev/seed/customer_services_seed.sql
psql ... -f dev/seed/services_seed.sql
psql ... -f dev/seed/mobile_trucks_seed.sql
```

then `dev/seed/topology_seed.py --fleet-config /etc/opengrid/sim/fleet.yaml --scada-config
/etc/opengrid/sim/scada.yaml --dsn "..."` last.

## 8. Enabled zone blocks

The repo's `integration-sims/config/{fleet,scada}.yaml` declare every zone block and ship them disabled. Production
enables the ones the owner approved through generated copies in `/etc/opengrid/sim/` plus drop-ins that point
`og-sim-fleet`/`og-sim-scada` at them ([README.md](README.md), "Austin (LZ_AEN) sim override"):

```bash
/opt/opengrid/venv/bin/python deploy/scripts/gen_sim_overrides.py --release <release_dir> \
    --out /etc/opengrid/sim --zones LZ_AEN [--dry-run]
```

It sets `enabled` on exactly the listed blocks, copies every other key unchanged, writes a header naming the
enabled blocks, and fails when a zone is not declared in both files. Output is deterministic, so a re-run is a
no-op. Phases f and e run it with `--zones` (`LZ_AEN`, plus `LZ_LCRA,LZ_RAYBN` with `--noie-blocks`). Seed those
hubs with `OG_FLEET_SIM_CONFIG=/etc/opengrid/sim/fleet.yaml python -m opengrid.fleet.seed`, then restart both
targets. Regenerate the copies on every release that changes either yaml.

The utility simulator (`og-sim-utility`, `ogsim.utility_aen`) has no generated copy: it reads the release's
`integration-sims/config/utility_aen.yaml` (`OGSIM_UTILITY_CONFIG` overrides the path, `OGSIM_UTILITY` the
utility). There AUSTIN_ENERGY is enabled and LCRA/RAYBURN are disabled, the channel is `customer_api`, and the
schedule places one 20,000 kW, 60-minute call between 16:30 and 18:00 on hot days. See
[the simulator README](../integration-sims/src/ogsim/utility_aen/README.md).

## 9. Data lifecycle

`deploy.sh` and phase j install `og-lifecycle.service`/`.timer` but never enable the timer. The service runs
`python -m opengrid.lifecycle run --cycle auto` (partitions, rollups, exports, retention) at idle I/O priority;
the timer fires every 10 minutes. Enable it only after an I/O check on `/srv/pgdata` (`iostat -dx 5`: util well
below 80 % under normal load): `systemctl enable --now og-lifecycle.timer`. Telemetry retention is 7 days
(`og.data_retention`). See [15-data-lifecycle.md](../docs/orchestrator/07-delivery/15-data-lifecycle.md).

This is separate from the engine's obligation lifecycle step (COMMITTED to DELIVERING, window-end
FULFILLED/SHORTFALL), which runs inside og-engine in the background as of r3.4.3.

## 10. Verify

- `systemctl is-active` for the 11 og-* units of the two targets (6 orchestrator: og-feeds, og-engine, og-guardian,
  og-safestop, og-settle, og-api; 5 simulators: og-sim-market, og-sim-fleet, og-sim-scada, og-sim-control,
  og-sim-utility); `GET /og/api/health` returns 200. Phase l prints ten of them (not og-sim-utility).
- `og.schema_migrations` holds every file in `orchestrator/migrations/` (through `0054_trace_delivery_record.sql`).
- Every hub is fresh within `health.hub_stale_s` (25 s at the 10 s telemetry cadence).
- All 12 invariants read 0 (`og.invariant_check`); a signed anchor appears in `/var/lib/opengrid/anchors`.
- `/og/` returns 401 without credentials and 200 for operator and viewer; `/ogsim/` returns 200 for tester.
- No degraded mode in `og.degraded_mode_state` once the ERCOT price feed and forecast are fresh. A fresh
  database needs about two weeks of price history for strict firm forecasts; until then the pooled rule
  applies, or backfill with `orchestrator/tools/ercot_backfill.py` (at most 6 requests per minute).
