# OpenGrid operations runbook (base server)

All commands run as `root` on the base server unless shown otherwise. There is no sudo. Never print
secrets: `/etc/opengrid/*.env`, `/root/opengrid-ui-credentials.txt` and the Apache proxy-secret conf are
read by scripts, never `cat`-ed. Stop and start services by **exact unit name** (never `pkill` by pattern).

## Units

| Unit | Role |
|---|---|
| `postgresql@17-main` | Postgres 17, data in `/srv/pgdata/17/main` (moved from `/var/lib/postgresql/17/main` on 2026-09-26; the old directory is kept as a fallback) |
| `mosquitto` | MQTT broker (config `/etc/mosquitto/conf.d/opengrid.conf`, ACL `/etc/mosquitto/opengrid.acl`) |
| `opengrid.target` | `og-engine`, `og-guardian`, `og-safestop`, `og-api`, `og-feeds`, `og-settle` |
| `ogsim.target` | `og-sim-fleet`, `og-sim-scada`, `og-sim-market`, `og-sim-control` (`og-sim-customer` is installed but not enabled until the customer services go live) |
| Apache vhost | `https://base.tocy-net.net/og/` -> `og-api` on `127.0.0.1:8080` (`deploy/apache/opengrid.conf`) |

Code lives at `/opt/opengrid/current` -> `/opt/opengrid/releases/<timestamp>`; the git checkout used to
build releases is `/opt/opengrid/src` (user `opengrid`).

## Start / stop

```bash
# start (Postgres and Mosquitto first)
systemctl start postgresql@17-main mosquitto
systemctl start opengrid.target ogsim.target

# stop (application first, then data)
systemctl stop ogsim.target opengrid.target
systemctl stop postgresql@17-main      # only for maintenance

# status of everything
for u in og-engine og-guardian og-safestop og-api og-feeds og-settle og-sim-fleet og-sim-scada og-sim-market og-sim-control; do
  printf '%s=%s ' "$u" "$(systemctl is-active "$u")"; done; echo
curl -fsS http://127.0.0.1:8080/og/api/health >/dev/null && echo api-ok
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

`deploy.sh` copies the release, runs forward-only migrations, re-seeds topology (idempotent), switches
`current`, restarts both targets, health-checks `/og/api/health` and **rolls back automatically** on any
failure. Without `--during-delivery` it refuses (exit 3) while a FIRM/AS obligation is delivering.

After a deploy, verify: all units active; `https://base.tocy-net.net/og/` loads for operator and viewer;
guardian verdicts mostly PASS; `og.invariant_check` shows 0 new violations; engine cycle p99 on
`http://127.0.0.1:9101/metrics` (`og_engine_cycle_latency_ms`).

## Rollback

```bash
ls -1t /opt/opengrid/releases           # newest first
bash /opt/opengrid/deploy/scripts/rollback.sh <release_timestamp>
```

Migrations are forward-only and additive: older code runs against the newer schema. Data-only migrations
(e.g. `0022_demo_as_ecrs.sql`) are not reverted by a rollback; reverse them by hand if needed.

Postgres data-directory move fallback: the pre-move `postgresql.conf` is saved as
`/etc/postgresql/17/main/postgresql.conf.pre-pgmove-<stamp>`. To go back to `/var/lib/postgresql/17/main`
(only valid if no writes happened after the move, or after re-syncing): stop the og units and
`postgresql@17-main`, restore that file over `postgresql.conf`, start Postgres, then the og units.

## Backup and restore

Nightly at 03:00 (`/etc/cron.d/opengrid`, user `opengrid`) `/opt/opengrid/deploy/scripts/backup.sh` writes
`og-YYYY-MM-DD.dump` (pg_dump custom format) to `/srv/ogbackup` (its own LV), keeping 7 days. On-demand:

```bash
runuser -u opengrid -- bash -lc 'set -a; . /etc/opengrid/secrets.env; set +a; /opt/opengrid/deploy/scripts/backup.sh'
```

Restore (tested by `tests/integration/platform/test_es01_s05_restore_and_verify.py`: dump -> restore into
a second database -> the trace hash chain still verifies):

```bash
# 1. restore into a scratch database first and verify it
runuser -u postgres -- createdb -O opengrid og_restore_check
runuser -u opengrid -- bash -lc 'set -a; . /etc/opengrid/secrets.env; set +a; export PGPASSWORD="$OG_DB_PASSWORD";
  pg_restore -h 127.0.0.1 -U opengrid -d og_restore_check --no-owner /srv/ogbackup/og-YYYY-MM-DD.dump'
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
end early with `DELETE /og/api/dispatch/as-deployments/{deployment_id}`; list with GET.

## Useful checks

```bash
# latest checkpoint (deploy right after one)
grep 'checkpoint complete' /var/log/postgresql/postgresql-17-main.log | tail -1
# engine latency
curl -s http://127.0.0.1:9101/metrics | grep og_engine_cycle_latency_ms
# recent errors
journalctl -u og-engine -u og-guardian -u og-settle -u og-api --since -10min -o cat | grep -E '"ERROR"|Traceback'
```
