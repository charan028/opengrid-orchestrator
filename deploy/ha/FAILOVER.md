# Single-host HA failover runbook

Owner-approved "light version" of HA (2026-09-26): a Postgres hot standby on the same host, on a
separate data directory/port (default 5433), set up by `deploy/ha/pg_standby_setup.sh`. This is a
**manual, operator-triggered** failover -- there is no automatic promotion, no Patroni/repmgr, no
VIP. It exists to recover from primary data-directory corruption or a disk failure on the primary's
volume without restoring from last night's `pg_dump` backup (`deploy/README.md` "Backup/restore",
which loses up to 24h). It does **not** protect against a whole-host failure (single host, by design
-- see `opengrid-docs-first-stage` decision: no Kubernetes for MVP-S).

**Before touching anything**, confirm with the lead — this is destructive to the old primary if steps
are done out of order (a `pg_rewind` case, not covered by this light-version runbook; if that's ever
needed, treat the old primary as if it doesn't exist and re-`pg_standby_setup.sh` a bran-new standby
from the new primary instead of trying to reconcile it).

## When to fail over

- The primary's Postgres process is unreachable/won't start, and the file system under its
  `PGDATA` is suspect (disk errors in `dmesg`/`journalctl`, `pg_ctl start` fails with I/O errors).
- NOT for "Postgres is just slow" or "the og-api unit crashed" -- those are `systemctl restart
  <unit>` (`deploy/README.md`), not a database failover.

## Preconditions to check first

```bash
# On the primary (if it's reachable at all):
systemctl status postgresql
journalctl -u postgresql -n 100 --no-pager

# On the standby: how far behind is it? (run on the standby itself)
sudo -u postgres psql -p 5433 -c "SELECT pg_last_wal_replay_lsn(), pg_is_in_recovery();"
# On the primary, if reachable, compare against pg_current_wal_lsn() from the monitoring query in
# pg_standby_setup.sh's final log output -- a large gap means recent commits will be lost.
```

If the primary is reachable and just needs a restart, **do that instead** -- a failover to the
standby loses any transaction committed on the primary after the standby's last received WAL.

## Failover steps

1. **Stop anything that could write to `og`** so nothing writes to a primary that's about to be
   abandoned, and nothing reads stale data mid-promotion:
   ```bash
   systemctl stop opengrid.target ogsim.target
   ```

2. **Confirm the primary is truly down** (don't promote a standby next to a live primary -- that's a
   split-brain with two writers). If the primary process is still running in any form, stop it first:
   ```bash
   systemctl stop postgresql   # on the OLD primary host, only if it's still up in some form
   ```

3. **Promote the standby**:
   ```bash
   sudo -u postgres pg_ctl promote -D /path/to/standby/pgdata   # the --standby-data-dir from setup
   # or, if running the standby as its own systemd unit (og-postgres-standby.service):
   sudo -u postgres pg_ctl promote -D "$PGDATA"   # PGDATA from that unit's Environment=
   ```
   Confirm promotion completed:
   ```bash
   sudo -u postgres psql -p 5433 -c "SELECT pg_is_in_recovery();"   # must now be 'f'
   ```

4. **Repoint every og-* process at the new primary.** `orchestrator/config/orchestrator.toml`'s
   `[postgres]` section (`host`/`port`) is what `opengrid.platform.db.build_dsn` reads --
   `OG_DB` (env) only overrides the *database name*, not host/port, so this is a config-file edit,
   not an env var:
   ```bash
   # On /opt/opengrid/current/orchestrator/config/orchestrator.toml:
   #   [postgres]
   #   host = "127.0.0.1"
   #   port = 5433              # <- was 5432; now points at the promoted former standby
   ```
   Edit the file in the current release directory (or better: in the release source and redeploy,
   per `deploy/README.md`, so the next `deploy.sh` doesn't clobber a hand-edit). Restart:
   ```bash
   systemctl start opengrid.target ogsim.target
   ```

5. **Verify the trace chain.** The promoted database must be internally consistent -- it was a
   replica a moment ago, so this confirms replication delivered a coherent copy, not a torn one:
   ```bash
   # From the app (once og-api is back up), or directly:
   curl -s -u operator:<pw> https://base.tocy-net.net/og/api/trace/verify -X POST | jq .
   # Expect ok:true for every stream. A broken chain here means the standby was behind or the
   # promotion raced a write -- stop immediately and get the lead + INVARIANTS owner involved
   # before letting og-engine/guardian resume producing new trace rows on top of a broken chain.
   ```
   Also spot-check heartbeats (`og.heartbeat`) and the Health screen show every process `ok`.

6. **Stand up a new standby** against the new primary as soon as practical -- the fleet has no
   redundancy again until this is done:
   ```bash
   ./deploy/ha/pg_standby_setup.sh --standby-data-dir <fresh dir, can reuse the old standby's disk
     after wiping PGDATA> --primary-port 5433
   ```
   (Point `--primary-port` at wherever the new primary now listens -- 5433 in this runbook's example,
   or back to 5432 if you also moved the promoted instance to the original port.)

## Rollback (if the "failed" primary turns out fine)

Do **not** just restart the old primary and point traffic back at it -- it has now diverged from the
promoted standby (both accepted no writes concurrently only if step 2 was followed correctly; if it
wasn't, treat divergence as certain). Either:
- Discard the old primary's data directory entirely and re-run `pg_standby_setup.sh` against it as a
  *new standby* of the promoted node, or
- If the old primary is provably ahead (never happened per step 2), get the lead involved before
  doing anything -- this is exactly the `pg_rewind` case this light-version runbook doesn't cover.
