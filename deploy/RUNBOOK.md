# OpenGrid operations runbook (base server)

All commands run as `root` on the base server unless shown otherwise. There is no sudo. Never print
secrets: `/etc/opengrid/*.env`, `/root/opengrid-ui-credentials.txt`, `/root/opengrid-utility-credentials.txt`
and the Apache proxy-secret conf are read by scripts, never `cat`-ed. Stop and start services by **exact unit
name** (never `pkill` by pattern). This page describes r3.4.3.

## Units

| Unit | Role |
|---|---|
| `postgresql@17-main` | Postgres 17, data in `/srv/pgdata/17/main` (moved from `/var/lib/postgresql/17/main` on 2026-09-26; the old directory is kept as a fallback) |
| `mosquitto` | MQTT broker (config `/etc/mosquitto/conf.d/opengrid.conf`, ACL `/etc/mosquitto/opengrid.acl`, passwords `/etc/mosquitto/opengrid.passwd`) |
| `opengrid.target` | `og-feeds`, `og-engine`, `og-guardian`, `og-safestop`, `og-settle`, `og-api` |
| `ogsim.target` | `og-sim-market`, `og-sim-fleet`, `og-sim-scada`, `og-sim-control`, `og-sim-utility` (r3.4.3; `og-sim-customer` is installed but not in the target and not enabled until the customer services go live) |
| `og-lifecycle.service` / `og-lifecycle.timer` | data lifecycle run every 10 min (oneshot, idle I/O); installed, timer **not** enabled by default (see "Data lifecycle") |
| Apache vhost | `https://base.tocy-net.net/og/` -> `og-api` on `localhost:8080` (`deploy/apache/opengrid.conf`) |

Unit files live in `deploy/systemd/`: `og-api`, `og-engine`, `og-feeds`, `og-guardian`, `og-safestop`,
`og-settle`, `og-lifecycle` (`.service` and `.timer`), `og-sim-control`, `og-sim-customer`, `og-sim-fleet`,
`og-sim-market`, `og-sim-scada`, `og-sim-utility`, `opengrid.target`, `ogsim.target`. Host drop-ins under
`/etc/systemd/system/<unit>.service.d/` are never touched by a deploy: `keys.conf` (per-key groups for
og-guardian, og-safestop, og-settle; [BOOTSTRAP.md](BOOTSTRAP.md) section 5), `austin.conf` (og-sim-fleet,
og-sim-scada zone-block configs) and `grid-link.conf` (og-engine, only while the grid link is enabled). Watchdog
drop-ins for HA are in `deploy/ha/systemd-dropins/`.

Code lives at `/opt/opengrid/current` -> `/opt/opengrid/releases/<timestamp>`; the git checkout used to
build releases is `/opt/opengrid/src` (user `opengrid`). The stable script copies are in
`/opt/opengrid/deploy/scripts` (`deploy.sh`, `rollback.sh`, `backup.sh`, installed by `install.sh`).

### Ports (all loopback)

| Port | Listener |
|---|---|
| 1883 | Mosquitto (production broker; `listener 1883` on localhost, no anonymous access) |
| 5432 / 5433 | Postgres production cluster / disposable test cluster (`og_t_*`, `og_test`) |
| 8080 | og-api (`[api].bind_port`), behind Apache |
| 8090 | og-sim-market (the ERCOT market simulator, incl. the MMS endpoint `/mms/ews/`) |
| 9101 / 9103 / 9106 | `/metrics` of og-engine / og-guardian / og-safestop (`[metrics]`, bind `localhost`) |
| 20001 (20002, 20003) | grid-link DNP3 outstation in og-engine for AUSTIN_ENERGY (LCRA, RAYBURN); nothing listens while the link is off (default) |

Test runs never use the production MQTT accounts: agent workspaces reach the 1883 broker only with their own
per-workspace users (`deploy/mosquitto/provision_ws_users.py`), and the performance stack (`tests-perf/`) runs
its own broker on 11883 and refuses to start with `MQTT_PORT` 1883.

## Start / stop

```bash
# start (Postgres and Mosquitto first)
systemctl start postgresql@17-main mosquitto
systemctl start opengrid.target ogsim.target

# stop (application first, then data)
systemctl stop ogsim.target opengrid.target
systemctl stop postgresql@17-main      # only for maintenance

# status of everything
for u in og-engine og-guardian og-safestop og-api og-feeds og-settle og-sim-fleet og-sim-scada og-sim-market og-sim-control og-sim-utility; do
  printf '%s=%s ' "$u" "$(systemctl is-active "$u")"; done; echo
curl -fsS http://localhost:8080/og/api/health >/dev/null && echo api-ok
```

Restarting `og-engine`/`og-guardian`/`og-sim-*` while a FIRM or AS obligation is DELIVERING interrupts
it (hubs hold their last setpoint for lease + hold, ~35 s). Prefer the minutes right after a Postgres
`checkpoint complete` line in `/var/log/postgresql/postgresql-17-main.log` (disk is quietest).

## Deploy a release

```bash
cd /opt/opengrid/src
runuser -u opengrid -- env GIT_SSH_COMMAND="ssh -F /opt/opengrid/.ssh/config" git pull --ff-only
REV=$(runuser -u opengrid -- git rev-parse --short HEAD)
rm -rf /root/release-$REV && mkdir /root/release-$REV
runuser -u opengrid -- git archive HEAD | tar -x -C /root/release-$REV
install -m 755 /root/release-$REV/deploy/scripts/deploy.sh /opt/opengrid/deploy/scripts/deploy.sh
bash /opt/opengrid/deploy/scripts/deploy.sh [--during-delivery] /root/release-$REV
```

What `deploy.sh` does, in order (log: `/var/log/opengrid/deploy.log`):

1. **Delivery preflight.** Lists obligations in state DELIVERING with service type ERCOT_AS, DIST_DEFERRAL,
   PARTNER_CAPACITY or DATA_CENTER. If any exist (or the query fails, reported as `UNKNOWN`), it stops with
   exit 3 unless `--during-delivery` is given, in which case it warns and proceeds.
2. Copies the release to `/opt/opengrid/releases/<timestamp>` (owned by `opengrid`).
3. Runs the forward-only migrations (`python -m opengrid.platform.db migrate`, as `opengrid`, sourcing
   `secrets.env` and `api_keys.env`).
4. Re-runs the fleet seed (`opengrid.fleet.seed`, idempotent upserts) from `/etc/opengrid/sim/fleet.yaml` when
   that generated override exists, else from the release's `integration-sims/config/fleet.yaml`. The topology
   seed is **not** re-run (see "Topology backfill").
5. Switches `current` to the new release.
6. Installs the release's `*.service`, `*.target` and `*.timer` files and reloads systemd (timers are never
   enabled here; host drop-ins are kept).
7. Creates the runtime directories (`/var/lib/opengrid`, `/var/lib/opengrid/anchors`, `/var/log/opengrid`, and
   `/srv/ogbackup/{anchors,cold}` when `/srv/ogbackup` exists).
8. Restarts `opengrid.target` and `ogsim.target`.
9. Health check: `GET /og/api/health` must answer within 30 s; then the post-restart check: every og-* service
   that either target pulls in (so `og-sim-utility` too) must be active, and each of `api engine feeds guardian
   safestop settle` must write an `og.heartbeat` newer than the restart, within about 90 s.
10. On any failure it **rolls back automatically**: `current` back to the previous release, that release's unit
    files reinstalled, both targets restarted, exit 1. On success it keeps the 5 newest releases.

After a deploy, verify: all units active; `https://base.tocy-net.net/og/` loads for operator and viewer;
guardian verdicts mostly PASS; `og.invariant_check` shows 0 new violations; engine cycle p99 on
`http://localhost:9101/metrics` (`og_engine_cycle_latency_ms`); og-safestop answers on
`http://localhost:9106/metrics`.

## Deploy from scratch

A new host, or a rebuild of this one after a total loss, uses `deploy/scripts/bootstrap_from_scratch.sh`. The
full procedure (storage and packages by hand, then phases a-l), every option, the owner-supplied inputs and the
expected counts are in [BOOTSTRAP.md](BOOTSTRAP.md); what each seed does is in
[dev/seed/README.md](../dev/seed/README.md). In short:

1. Storage, `postgresql-17 mosquitto apache2`, and the two venvs (BOOTSTRAP.md 1-2).
2. The owner places `/etc/opengrid/api_keys.env` (ERCOT/EIA) and, optionally, `/etc/opengrid/ai_agent.env`
   (Anthropic). Nothing else is supplied by hand: every other secret is generated on the host and never printed.
3. `git archive <tag> | tar -x -C /root/release-<tag>`, then from there:
   `bash deploy/scripts/bootstrap_from_scratch.sh --dry-run`, and without `--dry-run`.
4. Before phase l, install the utility API account and the utility simulator ("Utility API, utility simulator and
   grid link" below): `og-sim-utility` is part of `ogsim.target`, and `deploy.sh` rolls back when it is not active.
5. Expect: 11 og-* units active, `/og/api/health` 200, `/og/` 401 without credentials, every hub fresh,
   invariants 0, and only forecast degraded modes until the price history exists (phase k backfills it when the
   ERCOT keys are present). Enable `og-lifecycle.timer` only after the I/O check (BOOTSTRAP.md 9).

To prove a release seeds correctly without touching production: `make bootstrap-check` (fresh `og_t_boot` on the
test cluster, port 5433). `make schema-check` compares the migrations with the committed schema snapshot
(BOOTSTRAP.md, "Database schema").

## Rollback

```bash
ls -1t /opt/opengrid/releases           # newest first (deploy.sh keeps 5)
bash /opt/opengrid/deploy/scripts/rollback.sh <release_timestamp>
```

`rollback.sh` switches `current` to `/opt/opengrid/releases/<release_timestamp>`, restarts both targets and
waits up to 30 s for `/og/api/health`; on failure it logs "manual intervention needed" and exits 1. Unlike
`deploy.sh`'s automatic rollback it does **not** reinstall that release's unit files and does not run the
post-restart unit and heartbeat check. When the unit files differ between the two releases (r3.4.3 added
`og-sim-utility.service` to `ogsim.target`), install the target release's `deploy/systemd/*.service`,
`*.target` and `*.timer` into `/etc/systemd/system` and `systemctl daemon-reload` first, and check every unit
with the status loop above afterwards. A release older than r3.4.3 has no `ogsim.utility_aen`, so
`og-sim-utility` cannot run under it.

Migrations are forward-only and additive: older code runs against the newer schema. Data-only migrations
(e.g. `0022_demo_as_ecrs.sql`) are not reverted by a rollback; reverse them by hand if needed.

Postgres data-directory move fallback: the pre-move `postgresql.conf` is saved as
`/etc/postgresql/17/main/postgresql.conf.pre-pgmove-<stamp>`. To go back to `/var/lib/postgresql/17/main`
(only valid if no writes happened after the move, or after re-syncing): stop the og units and
`postgresql@17-main`, restore that file over `postgresql.conf`, start Postgres, then the og units.

## Backup and restore

Nightly at 03:00 (`/etc/cron.d/opengrid`, user `opengrid`, output appended to `/var/log/opengrid/backup.log`)
`/opt/opengrid/deploy/scripts/backup.sh` writes `og-YYYY-MM-DD.dump` (`pg_dump --format=custom` of `og` on 5432)
to `/srv/ogbackup` (its own LV; `OG_BACKUP_DIR` overrides it). It writes `<file>.in-progress` first and renames it
when complete, sets mode 640, and deletes dumps older than 7 days. It needs `OG_DB_PASSWORD` in the environment
and never prints it; `og_test` and the `og_t_*` workspace databases are never backed up. On-demand:

```bash
runuser -u opengrid -- bash -lc 'set -a; . /etc/opengrid/secrets.env; set +a; /opt/opengrid/deploy/scripts/backup.sh'
```

Restore (tested by `tests/integration/platform/test_es01_s05_restore_and_verify.py`: dump -> restore into
a second database -> the trace hash chain still verifies):

```bash
# 1. restore into a scratch database first and verify it
runuser -u postgres -- createdb -O opengrid og_restore_check
runuser -u opengrid -- bash -lc 'set -a; . /etc/opengrid/secrets.env; set +a; export PGPASSWORD="$OG_DB_PASSWORD";
  pg_restore -h localhost -U opengrid -d og_restore_check --no-owner /srv/ogbackup/og-YYYY-MM-DD.dump'
#    check row counts / run the trace verify against og_restore_check
# 2. replace production (downtime): stop the application, then
systemctl stop ogsim.target opengrid.target
runuser -u postgres -- psql -c "ALTER DATABASE og RENAME TO og_before_restore"
runuser -u postgres -- psql -c "ALTER DATABASE og_restore_check RENAME TO og"
systemctl start opengrid.target ogsim.target
```

Keep `og_before_restore` until the restored system is verified; drop it only after that.

## Safe stop (K8) and release

A safe stop is engaged in two steps and released by **two different authorised operators**; the guardian
signs the RELEASE, og-safestop publishes it (`og.stop_event`, retained `og/v1/stop/<scope>/<id>/<stop_id>`).

UI: Fleet screen -> Safe stop (propose, then confirm). Release: operator A requests, operator B approves.

API (through Apache, Basic auth as an operator):

```text
POST /og/api/safestop                         {"scope":"bank|zone|fleet","scope_id":"bank-034","reason":"..."}
POST /og/api/safestop/{proposal_id}/confirm   -> 200 engaged
POST /og/api/safestop/{scope}/{scope_id}/release   {"reason":"..."}      (operator A)
POST /og/api/safestop/release/{proposal_id}/approve                      (operator B; A gets 403)
```

Approve answers 200 when the guardian's signed RELEASE lands, 202 while pending. The guardian refuses
(`GUARDIAN_VERDICT kind=STOP_RELEASE outcome=REFUSED`, reason in the payload) for an unauthorised
operator, the same person twice, a stale approval, or an active utility ESTOP/BLOCK on the scope.
Authorised operators: `[guardian].stop_release_authorised_operators` in `orchestrator.toml`.

og-safestop serves `/metrics` on `[metrics].safestop_port` (9106, bound to `[metrics].bind_host`, localhost),
e.g. `og_mqtt_reconnects_total{client="safestop"|"safestop-l2"}` and `og_mqtt_connected`:
`curl -s http://localhost:9106/metrics | grep og_mqtt`.

### Operator accounts for the two-person release

`deploy/scripts/install.sh` creates an Apache account for every operator in
`[guardian].stop_release_authorised_operators` (today the D-12 test accounts `og-op-a`/`og-op-b`), with
generated passwords appended to `/root/opengrid-ui-credentials.txt` (0600, never printed). Before real
operation, replace them with real per-person accounts and keep three places in sync:

1. `/etc/opengrid/htpasswd`: `htpasswd -B /etc/opengrid/htpasswd <person>` (and `htpasswd -D` the test
   accounts);
2. `orchestrator.toml`: `[guardian].stop_release_authorised_operators` (who may request/approve a release)
   and `[api.roles].operator` (who may use operator routes);
3. `deploy/apache/opengrid.conf`: the `Require user` line of the `/og/` block.

Then deploy (the guardian and og-api read the config at start) and `apache2ctl configtest && systemctl
reload apache2`. Two different people are always required: the guardian refuses a release approved by its
requester.

**The shared `operator` account cannot complete a release.** It can engage a safe stop and request a
release, but it is not in `stop_release_authorised_operators`, so the guardian refuses any release it
requests or approves. One shared login is also one identity, which can never be both people. Releases
need two named accounts (today `og-op-a` and `og-op-b`).

The engage proposal lives for `[safestop].confirm_window_s` (30 s); a confirm after that returns 409
"proposal expired, propose again". Release requests keep their 60 s window.

## ERCOT AS deployment (demo trigger)

An ERCOT_AS award is a 0 kW capacity hold until deployed. To deploy one award (operator):
`POST /og/api/dispatch/as-deployments {"obligation_id": "<id>", "duration_minutes": 15, "reason": "..."}`
(duration capped by the award's product: ECRS 60 min, Non-Spin 240; fleet-wide deployment is refused);
end early with `DELETE /og/api/dispatch/as-deployments/{deployment_id}`; list with GET. The operator path goes
through the same core call function (`opengrid.calls`) as a utility call and the AS poller, and settlement prices
the capacity at the cleared DAM MCPC for the delivery hour (flag `MCPC`; without an observation the opportunity's
own price, flag `OPPORTUNITY_PRICE`).

### ERCOT AS deployment poller (AS-POLL, D-35)

og-feeds can poll ERCOT AS deployment instructions (DEPLOY_AS / RECALL_AS for ECRS, RRS, Reg, Non-Spin) over the
`ercot_mms` adapter and apply each through the same core as the operator endpoint above. It is **off** in the repo
(`[feeds.ercot_as_poll].enabled = false`). Settings: `interval_s` 5, `retry_cap_s` 60, `failure_alert_after` 3,
`stale_after_s` 60 (must exceed `interval_s`), `lookback_s` 900, `max_instruction_age_s` 300, `recall_hold_s` 300.
Enabling needs `[feeds.ercot_as_poll.mms]` (against the simulator: `endpoint = "http://localhost:8090/mms/ews/"`,
`qse_code`, `user_id`, `signing = "none"`) and `[feeds.ercot_as_poll.awards]` (`"<resource>:<AS type>"` = the
ERCOT_AS contract id). There is no host override file for this table: set it in the release's
`orchestrator/config/orchestrator.toml` before `deploy.sh` (a hand edit under `/opt/opengrid/current` is lost on
the next deploy), then `systemctl restart og-feeds`. Without a trace backend the poller does not start and logs
"ERCOT AS instruction poller NOT started". Every instruction is traced (stream `ercot_as_poll`). Alerts:
`ALR-ERCOT-AS-REFUSED` (per refused instruction), `ALR-ERCOT-AS-POLL-FAILED` (after 3 failed polls),
`ALR-ERCOT-AS-POLL-STALE` (no good poll for 60 s).

## Delivery verification (D-38)

As of r3.4.3 og-settle runs the delivery job (`opengrid.delivery`) every `[delivery].interval_s` (15 s) in
30 s buckets, after a `telemetry_lag_s` of 30 s, for every discharge call (utility toll call, ERCOT AS deployment,
operator manual discharge target) until it has a final record, up to `lookback_s` (1 h) after the call ended.
Records are in `og.delivery_record` (migration 0050): committed, commanded (from signed batches) and delivered
(from telemetry) per bucket, and meter versus battery on metered banks (SUBSTATION asset banks always, plus
`[delivery].meter_bank_ids`). Final records are traced (`DELIVERY_VERIFICATION`). The job observes only (K7): it
never commands a hub. `[delivery].enabled = false` turns it off (restart og-settle).

- Live alerts: `ALR-DELIVERY-RAMP-LATE` (target not reached within `[delivery.ramp_time_s]` for the product),
  `ALR-DELIVERY-SHORTFALL`, `ALR-DELIVERY-NONE` (each after 60 s), `ALR-DELIVERY-METER-MISMATCH` (meter and battery
  differ beyond `meter_tolerance_frac` 0.10, with a `meter_floor_kw` of 25 kW).
- A measured SHORTFALL or NONE flags the obligation **AT_RISK** (`R-DELIVERY-MEASURED-SHORTFALL`); it is cleared
  on recovery (`R-DELIVERY-RECOVERED`).
- Operator API: `GET /og/api/delivery/records` (filters), `GET /og/api/delivery/records/{call_id}` (with the
  series), `GET /og/api/delivery/summary`. Call status (RAMPING/DELIVERING) now follows measured delivered kW.

Check the job: `journalctl -u og-settle --since -10min -o cat | grep -i delivery`. What to do on a delivery alert
is in the Help playbooks (`pb-delivery-alert`).

## Utility API, utility simulator and grid link

These are installed by release-manager scripts in `deploy/scripts/`, each idempotent, a **dry run by default**
(`APPLY=1` or `--apply` applies) and never printing a secret.

**Order for an r3.4.3 deploy (required):** 1. the Apache script (`r341_utility_api_apache.sh`), 2. the
utility simulator install (`r343_utility_sim_install.sh`: MQTT user and `utility_sim.env`), 3. then
`deploy.sh`. `ogsim.target` wants `og-sim-utility`, which cannot start without `/etc/opengrid/utility_sim.env`
and its MQTT user; without them the post-restart check fails and `deploy.sh` rolls back (seen on the first
r3.4.3 attempt). The utility API itself (D-33) is served by og-api
under `/og/api/customer/v1/utility/` for utilities in `[api.utility_api].enabled_utilities` (today
AUSTIN_ENERGY only).

1. **Utility API account** (`r341_utility_api_apache.sh`): creates the Apache account `og-util-aen` (bcrypt)
   with a generated password written only to `/root/opengrid-utility-credentials.txt` (600) and
   `/etc/opengrid/utility_sim.env` (640 root:opengrid), makes sure the `<Location /og/api/customer/>` `Require
   user` line lists it (the repo's `deploy/apache/opengrid.conf` already does), then configtest and reload;
   backups are restored on a failed configtest. `APPLY=1 bash deploy/scripts/r341_utility_api_apache.sh`.
2. **Utility simulator** (`r343_utility_sim_install.sh`, after step 1): creates the MQTT user `og_sim_utility`
   (password into `utility_sim.env` as `OG_MQTT_UTILITY_PASSWORD`), an ACL block allowing it only to read
   `og/v1/scenario/cmd`, reloads Mosquitto (restoring backups if it is not active afterwards), then installs
   `og-sim-utility.service` and `ogsim.target` from `SRC` (default `/opt/opengrid/current/deploy/systemd`) and
   `systemctl enable --now og-sim-utility`. `APPLY=1 bash deploy/scripts/r343_utility_sim_install.sh`.
   `og-sim-utility` runs `ogsim.utility_aen` (the simulated Austin Energy EMS) with
   `integration-sims/config/utility_aen.yaml`: channel `customer_api`, one 20,000 kW, 60-minute call between
   16:30 and 18:00 on hot days, and the five `/ogsim/` utility scenarios. See
   [the simulator README](../integration-sims/src/ogsim/utility_aen/README.md).
3. **Grid link** (D-34, `grid_link_enable_loopback.sh`): the DNP3 link is **disabled by default**
   (`[grid_link].enabled = false`, every utility `enabled = false`). The script enables AUSTIN_ENERGY on
   localhost port 20001 only, with a locally generated test PKI in `/etc/opengrid/certs`, the MQTT user
   `og_gridlink` (publish only on `og/v1/scada/instruction/#`, through
   `deploy/mosquitto/provision_grid_link_user.py`, password from `GL_MQTT_PASSWORD`, never argv), the override
   `/etc/opengrid/grid_link.toml` and the og-engine drop-in `grid-link.conf` (`OG_GRID_LINK_CONFIG`); it restarts
   og-engine and waits for the port. The release config is never edited; LCRA and RAYBURN stay off.
   og-engine starts the link at startup (`start_grid_link` in `engine/__init__.py`) whenever `[grid_link].enabled`
   and at least one utility entry's `enabled` are true; a link error is logged ("grid link task stopped") and
   never stops the engine. Check with `journalctl -u og-engine | grep -i 'grid link'` and `ss -ltn | grep 20001`.

   ```bash
   bash deploy/scripts/grid_link_enable_loopback.sh                      # dry run: the plan
   bash deploy/scripts/grid_link_enable_loopback.sh --apply --test-call  # enable, then one 100 kW test call via the AE sim channel
   bash deploy/scripts/grid_link_enable_loopback.sh --disable --apply    # link off again
   ```

   Other flags: `--no-restart`, `--rotate-certs`, `--keep-call`, `--kw N`. Outside the toll window the core
   refuses the test call (e.g. `R-CALL-OUTSIDE-WINDOW`), which still proves the link end to end. L2 LIMIT/BLOCK
   levels are restored across og-engine restarts from the link trace. Design, point list and configuration:
   [grid-link.md](../docs/orchestrator/07-delivery/integrations/grid-link.md).

## Topology backfill

A database seeded before r3.4.1 lacks grid-topology rows (service transformers, hub mappings, feeder and
substation limits, HOME_BANK assets, the POI premise of the substation set and the trucks), which shows as
ALR-XFMR-UNMAPPED / ALR-BANK-UNMAPPED-TOPOLOGY. `deploy.sh` does not add them. `topology_backfill.sh` runs
`dev/seed/topology_seed.py --only-missing` with the generated sim configs in `/etc/opengrid/sim`:

```bash
bash deploy/scripts/topology_backfill.sh                          # dry run (rolled back): rows per statement, unmapped before -> after
bash deploy/scripts/topology_backfill.sh --apply                  # commit
bash deploy/scripts/topology_backfill.sh --only sub-LZ_AEN-00     # just that hub's bank (dry run unless --apply)
```

It is insert-only: it never rewrites an existing bank, hub, limit, transformer or asset row (checked inside its
transaction, plus LZ_AEN bank/hub checksums before and after), and a second `--apply` inserts 0 rows. Other
options: `--release`, `--etc`, `--db-port`, `--db-name`, `--db-role`. The guardian picks the rows up within its
60 s topology refresh; no restart is needed.

## Data lifecycle

`og-lifecycle.service` runs `python -m opengrid.lifecycle run --cycle auto` (partitions, rollups, exports,
retention) as `opengrid` at `Nice=10`, idle I/O class, `IOWeight=10`, `MemoryMax=512M`, up to 50 min; the timer
runs it 5 min after boot and every 10 min. `deploy.sh` installs both but never enables the timer. Enable it only
after an I/O check on `/srv/pgdata` (BOOTSTRAP.md 9): `systemctl enable --now og-lifecycle.timer`; check a run
with `journalctl -u og-lifecycle -n 50`. See
[15-data-lifecycle.md](../docs/orchestrator/07-delivery/15-data-lifecycle.md).

The engine's own obligation lifecycle step (COMMITTED to DELIVERING, window-end FULFILLED/SHORTFALL, expiry of
unselected opportunities) runs inside og-engine, in the background as of r3.4.3: single-flight (a cycle is skipped,
never queued, while the previous pass runs: "obligation lifecycle pass still running") and abandoned after 30 s
("obligation lifecycle pass timed out"); dispatch keeps ticking either way. The guardian hand-off is bounded too
(`[allocator].propose_timeout_s` 2 s, `[allocator].guardian_check_timeout_s` 0.5 s): banks cut off hold their last
signed command.

## Useful checks

```bash
# latest checkpoint (deploy right after one)
grep 'checkpoint complete' /var/log/postgresql/postgresql-17-main.log | tail -1
# engine latency; safestop and guardian metrics
curl -s http://localhost:9101/metrics | grep og_engine_cycle_latency_ms
curl -s http://localhost:9106/metrics | grep og_mqtt_connected
curl -s http://localhost:9103/metrics | head
# recent errors
journalctl -u og-engine -u og-guardian -u og-settle -u og-api -u og-feeds --since -10min -o cat | grep -E '"ERROR"|Traceback'
# utility simulator
journalctl -u og-sim-utility --since -1h -o cat | tail -20
```
