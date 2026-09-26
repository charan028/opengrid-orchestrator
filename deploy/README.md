# OpenGrid Orchestrator — deploy runbook

Owned by the `deploy` role (BUILD.md). Covers everything installed by
`deploy/scripts/install.sh` on `basepower` (192.168.5.35): systemd units, the Apache
reverse-proxy config, backups, and log rotation. See `02b-mvp-s-spec-platform.md` §9 for the
design this implements.

**Status as of this install:** systemd units are installed but **not enabled/started** — the
application code (`opengrid`, `ogsim` packages) does not exist yet. Nothing under `/opt/opengrid`
has a `current` symlink or a release. The Apache route and basic auth are live now (they don't
need the app to exist to be verified — see "Verification" below), so `/og/` and `/ogsim/`
correctly return 401 without credentials today, and will return 502/503 with credentials until
a release is deployed and the units are started.

## Processes

| Unit | Package | Entry point | Memory budget |
|---|---|---|---|
| `og-feeds` | orchestrator | `opengrid.feeds` | 512M max / 410M high |
| `og-engine` | orchestrator | `opengrid.engine` | 2G max / 1638M high |
| `og-guardian` | orchestrator | `opengrid.guardian` | 512M max / 410M high |
| `og-safestop` | orchestrator | `opengrid.safestop` | 256M max / 205M high |
| `og-settle` | orchestrator | `opengrid.settle` | 512M max / 410M high |
| `og-api` | orchestrator | `opengrid.api` | 1G max / 819M high |
| `og-sim-market` | integration-sims | `ogsim.market` | 384M max / 307M high |
| `og-sim-fleet` | integration-sims | `ogsim.fleet` | 3G max / 2458M high (2,000 hub asyncio tasks) |
| `og-sim-scada` | integration-sims | `ogsim.scada` | 384M max / 307M high |
| `og-sim-control` | integration-sims | `ogsim.control` | 256M max / 205M high |

Orchestrator total MemoryMax ≈ 4.75 GB; sims total ≈ 4.0 GB (same envelope as the spec's single
`og-sim` process, split four ways because this repo runs `market`/`fleet`/`scada`/`control` as
separate `ogsim` processes). Both fit comfortably under the host's ~16 GB with Postgres/Apache/mail
already using ~2 GB observed.

`opengrid.target` groups the six orchestrator units; `ogsim.target` groups the four sim units.
Each service has `PartOf=<target>`, so `systemctl restart <target>` restarts every unit in it —
this is what `deploy.sh`/`rollback.sh` use. `og-safestop` has no `After=`/`Requires=` on
`og-engine` or `og-guardian` (invariant K8: the scoped stop must work even if both are down).

## Start / stop / status

There is no `sudo` on this host; everything below runs as root over SSH (the deploy role's key),
never via a script the API calls at runtime.

```bash
systemctl status opengrid.target ogsim.target
systemctl start   opengrid.target ogsim.target   # only once app code + a release exist
systemctl stop    opengrid.target ogsim.target
systemctl restart og-engine                      # any single unit works the same way
journalctl -u og-engine -f                        # tail one process's log
```

## Deploy

```bash
# on your workstation: produce a release directory containing orchestrator/ and
# integration-sims/ (a tagged checkout), then copy it to the server, e.g. under /root/.
scp -r ./release-v0.1.0 root@192.168.5.35:/root/release-v0.1.0
ssh root@192.168.5.35 bash /opt/opengrid/deploy/scripts/deploy.sh /root/release-v0.1.0
```

`deploy.sh <release_dir>`:
1. Copies `release_dir` to `/opt/opengrid/releases/<timestamp>`, owned by `opengrid`.
2. Runs `python -m opengrid.platform.db migrate` as `opengrid` against the new release.
3. Atomically re-points `/opt/opengrid/current` at the new release.
4. `systemctl restart opengrid.target ogsim.target`.
5. Polls `GET http://127.0.0.1:8080/og/api/health` for up to 30 s.
6. On any failure in steps 2–5, automatically re-points `current` back at the previous release,
   restarts the targets again, and exits non-zero. Logs to `/var/log/opengrid/deploy.log`.
7. Keeps the 5 most recent releases under `/opt/opengrid/releases/`, prunes older ones.

Migrations are forward-only for MVP-S (additive columns/tables only, never a rename/drop in one
migration) specifically so a rollback to older code tolerates a schema that is slightly ahead of
it (§9.6).

## Rollback

```bash
ls /opt/opengrid/releases/                 # find the release name (timestamp) to go back to
ssh root@192.168.5.35 bash /opt/opengrid/deploy/scripts/rollback.sh <release_name>
```

Re-points `current`, restarts both targets, health-checks the same way as `deploy.sh` step 5,
but does **not** run migrations and does **not** roll back the database — see "Restore" below if
a bad migration needs undoing.

## Backup / restore

- **Backup**: `/etc/cron.d/opengrid` runs `/opt/opengrid/deploy/scripts/backup.sh` nightly at
  03:00 as `opengrid`, producing `/var/lib/opengrid/backups/og-<YYYY-MM-DD>.dump`
  (`pg_dump --format=custom`), keeping 7 days. `og_test` and the per-workspace `og_t_*` databases
  (used by `tools/remote.ps1`) are never backed up — disposable.
- **Manual backup**: `runuser -u opengrid -- bash -c 'set -a; . /etc/opengrid/secrets.env; set +a; /opt/opengrid/deploy/scripts/backup.sh'`
- **Restore**:
  ```bash
  systemctl stop opengrid.target ogsim.target
  runuser -u opengrid -- bash -c "
    set -a; . /etc/opengrid/secrets.env; set +a
    export PGPASSWORD=\$OG_DB_PASSWORD
    dropdb --host=127.0.0.1 --username=opengrid og
    createdb --host=127.0.0.1 --username=opengrid --owner=opengrid og
    pg_restore --host=127.0.0.1 --username=opengrid --dbname=og /var/lib/opengrid/backups/og-<DATE>.dump
  "
  systemctl start opengrid.target ogsim.target
  ```
  Confirm with the lead before dropping `og` — this is destructive to current data.

## Logs

- `/var/log/opengrid/*.log` — application logs (rotated daily, 14 kept, by
  `/etc/logrotate.d/opengrid`), plus `deploy.log` and `backup.log` from the scripts above.
- `journalctl -u <unit>` — stdout/stderr and lifecycle events for any of the 10 units.

## UI credentials

Randomly generated on install; **never printed or committed**. Location only:
`/root/opengrid-ui-credentials.txt` (mode 600, root-only). Contains `operator`/`viewer` (for
`https://base.tocy-net.net/og/`) and `tester` (for `https://base.tocy-net.net/ogsim/`).
Regenerate a single user's password with `htpasswd -B /etc/opengrid/htpasswd <user>` (root only;
update the credentials file by hand afterwards).

## MQTT ACL (production vs. test workspaces)

`/etc/mosquitto/opengrid.acl` grants each `og_*` user only its own least-privilege topic set on the
production root `og/v1/...`. It does **not** grant blanket access to the test root `ogtest/<ws>/...`
that `tools/remote.ps1`'s per-workspace test runs use (qa/security-review.md F-04: the earlier blanket
`pattern readwrite ogtest/#` applied to every authenticated user unconditionally, including `og_api`,
which should be read-only, and was a residual open surface once this stopped being a pure dev/test
deployment).

**Running `tools/remote.ps1`-based server tests therefore needs the test root re-enabled first.** Two
ways to do this, in order of preference:

1. Add a dedicated test-only Mosquitto user (e.g. `og_test`) scoped to `pattern readwrite ogtest/#`
   via its own ACL block, and point the test workspace's MQTT auth at that user instead of the
   production `og_*` accounts. Not done as part of this pass (needs a new Mosquitto password to be
   generated and distributed to `tools/remote.ps1`/`secrets.env`, which is a credential-provisioning
   step, not a config edit) -- left for whoever next needs `ogtest/#` access to do properly.
2. **Temporary manual toggle** (what to do until (1) exists): re-add `pattern readwrite ogtest/#` as
   the first line of `/etc/mosquitto/opengrid.acl`, `systemctl restart mosquitto`, run the test
   workspace, then remove that line and restart `mosquitto` again. Never leave it in place outside an
   active test session.

## Resource budgets

See the table above for `MemoryMax`/`MemoryHigh` per unit (`02b-mvp-s-spec-platform.md` §9.3/§11
memory budgets). Real-time cycle p99 < 500 ms at 2,000 hubs and UI refresh ≤ 2 s are engine/API
concerns, not deploy's, but both are exported on `/metrics` per process and on the Health screen
once the app runs.

## Switching live APIs vs. the market simulator

Configuration only, never a code path (`orchestrator/config/orchestrator.toml`, `[feeds.*]`
sections and the `og-sim-market` fixture/replay settings) — no deploy-side action needed beyond
restarting `og-feeds` (and `og-sim-market` if its replay corpus changed) after the config edit:

```bash
systemctl restart og-feeds        # picks up a config.toml [feeds.*] change
systemctl restart og-sim-market   # picks up a change to the simulated market fixture/replay mode
```

## Apache route

`/etc/apache2/conf-available/opengrid.conf` (enabled via `a2enconf opengrid`) proxies:
- `/og/` → `http://127.0.0.1:8080/og/` (og-api), Basic Auth (`operator`, `viewer`), SSE-friendly
  (`proxy-sendchunked`, `flushpackets=on`, no compression).
- `/ogsim/` → `http://127.0.0.1:8091/` (ogsim.control), Basic Auth (`tester`).

Both set `X-Remote-User` from the authenticated identity after stripping any client-supplied
value. This conf is a conf-enabled fragment (not a vhost edit) — Apache merges its `<Location>`
blocks into every vhost, including `sites-enabled/base-ssl.conf` (`base.tocy-net.net`). No vhost
file was modified. Any config change: `apache2ctl configtest` **before** `systemctl reload
apache2`; if configtest fails, revert and stop (never reload a config that fails configtest).

`www-data` (the Apache user) needs to traverse into `/etc/opengrid` (mode `750`, `root:opengrid`)
to open `htpasswd`. `install.sh` grants that with a POSIX ACL (`setfacl -m u:www-data:x
/etc/opengrid`, falling back to `chmod o+x` if `setfacl` is unavailable) — search-only, so
`www-data` still cannot list the directory or read `secrets.env`/`api_keys.env` (those stay mode
`640 root:opengrid`, no ACL entry for `www-data`).
