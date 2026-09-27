# Bootstrap a base server from scratch

This is the order that rebuilds a production node like `basepower` (192.168.5.35) from an empty Debian host.
Everything runs as root over SSH. Secrets are generated on the server and never printed, committed or
pasted into a chat. `deploy/README.md` and `deploy/RUNBOOK.md` cover day-to-day operation; this page covers
only the first build.

Time: about 45 minutes, most of it the Postgres initialisation and the first fleet seed.

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
