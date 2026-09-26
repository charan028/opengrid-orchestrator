#!/bin/bash
# deploy/ha/pg_standby_setup.sh
#
# Single-host HA, "light version" (owner-approved 2026-09-26): a Postgres streaming hot standby on
# the SAME host as the primary, in its own data directory and on its own port (default 5433) --
# ideally on a separate disk, since the owner is adding VM capacity for exactly this. This is NOT
# Patroni/repmgr and does not fail over automatically; promotion is a deliberate operator action
# (see deploy/ha/FAILOVER.md). It buys: (1) a warm copy of the data a disk failure on the primary's
# volume doesn't touch, (2) a promotable replica the owner can point OG_DB at by hand within minutes.
#
# NOT executed by this script's authors. The owner or lead runs it (root, on basepower) after
# reviewing the parameters below and the primary's postgresql.conf (must already have
# wal_level=replica, max_wal_senders>=2, max_replication_slots>=2 -- this script does not edit the
# primary's config; see the "Primary prerequisites" check below, which only verifies, never sets).
#
# Idempotent: re-running after a partial failure is safe -- every step first checks whether its
# result already exists and skips if so. --dry-run prints every command it would run (including the
# exact `pg_basebackup`/`createuser`/`psql` invocations) without executing any of them.
#
# Usage:
#   pg_standby_setup.sh --standby-data-dir /mnt/standby-disk/pgdata_standby [options]
#
# Options (all have defaults suitable for basepower's current layout; override for the new disk):
#   --standby-data-dir DIR     REQUIRED. Standby's PGDATA (put this on the new disk/volume).
#   --standby-port PORT        Standby's listen port (default: 5433).
#   --primary-host HOST        Primary's host, as the standby connects to it (default: 127.0.0.1 --
#                               same host; a future off-host standby would need the primary to also
#                               listen on more than loopback and pg_hba.conf updated accordingly,
#                               which is out of scope for "single-host HA").
#   --primary-port PORT        Primary's port (default: 5432, config/orchestrator.toml [postgres].port).
#   --replication-user NAME    Dedicated replication role (default: og_replicator). Created with
#                               REPLICATION LOGIN only -- never superuser, never able to touch `og`'s
#                               tables directly.
#   --replication-slot NAME    Physical replication slot name (default: og_standby_slot). A slot
#                               stops the primary from recycling WAL the standby still needs, at the
#                               cost of primary disk filling up if the standby is down for a long
#                               time -- monitor `pg_replication_slots.active` (the monitoring query
#                               below) and drop/recreate the slot if the standby is decommissioned.
#   --postgres-bin-dir DIR     Directory holding pg_basebackup/initdb/pg_ctl if not on PATH.
#   --dry-run                  Print every command; execute nothing; still exits 0.
#
# Requires: run as a user that can sudo to `postgres` (or already be `postgres`), and PGPASSWORD (or
# a .pgpass entry) for connecting to the primary as the postgres superuser to run the one-time setup
# SQL (CREATE ROLE, CREATE SLOT). The replication PASSWORD itself is read from
# OG_REPLICATOR_PASSWORD in the environment -- never generated or printed by this script (same
# secrets-by-name discipline as orchestrator/src/opengrid/platform/config.py's `resolve_secret`).

set -euo pipefail

STANDBY_DATA_DIR=""
STANDBY_PORT=5433
PRIMARY_HOST=127.0.0.1
PRIMARY_PORT=5432
REPLICATION_USER=og_replicator
REPLICATION_SLOT=og_standby_slot
POSTGRES_BIN_DIR=""
DRY_RUN=0

log() { echo "[pg_standby_setup] $*"; }

run() {
    if [ "$DRY_RUN" = "1" ]; then
        echo "+ $*"
    else
        "$@"
    fi
}

usage() { sed -n '2,45p' "$0"; exit 1; }

while [ $# -gt 0 ]; do
    case "$1" in
        --standby-data-dir) STANDBY_DATA_DIR="$2"; shift 2 ;;
        --standby-port) STANDBY_PORT="$2"; shift 2 ;;
        --primary-host) PRIMARY_HOST="$2"; shift 2 ;;
        --primary-port) PRIMARY_PORT="$2"; shift 2 ;;
        --replication-user) REPLICATION_USER="$2"; shift 2 ;;
        --replication-slot) REPLICATION_SLOT="$2"; shift 2 ;;
        --postgres-bin-dir) POSTGRES_BIN_DIR="$2"; shift 2 ;;
        --dry-run) DRY_RUN=1; shift ;;
        -h|--help) usage ;;
        *) echo "unknown argument: $1" >&2; usage ;;
    esac
done

if [ -z "$STANDBY_DATA_DIR" ]; then
    echo "error: --standby-data-dir is required (put it on the new disk)" >&2
    exit 1
fi

if [ -n "$POSTGRES_BIN_DIR" ]; then
    PATH="$POSTGRES_BIN_DIR:$PATH"
fi

PSQL_PRIMARY=(psql --host="$PRIMARY_HOST" --port="$PRIMARY_PORT" --username=postgres --no-password)

log "Primary prerequisites (verify only -- never modified by this script):"
for setting in wal_level max_wal_senders max_replication_slots; do
    value="$("${PSQL_PRIMARY[@]}" -tAc "SHOW $setting;" 2>/dev/null || echo "<unreachable>")"
    log "  $setting = $value"
done
log "  If wal_level != replica, or max_wal_senders/max_replication_slots < 2: edit the primary's"
log "  postgresql.conf by hand and 'systemctl restart postgresql' BEFORE continuing -- this script"
log "  does not do that for you (a primary restart is not something to hide inside a standby script)."

# --- 1. Replication role on the primary (idempotent) -------------------------------------------------
ROLE_EXISTS="$("${PSQL_PRIMARY[@]}" -tAc "SELECT 1 FROM pg_roles WHERE rolname='$REPLICATION_USER';" 2>/dev/null || echo "")"
if [ "$ROLE_EXISTS" = "1" ]; then
    log "Role '$REPLICATION_USER' already exists, skipping."
else
    : "${OG_REPLICATOR_PASSWORD:?OG_REPLICATOR_PASSWORD must be set (the replication role's password; never logged)}"
    log "Creating replication role '$REPLICATION_USER' (REPLICATION LOGIN, no superuser, no table access)."
    run "${PSQL_PRIMARY[@]}" -c \
        "CREATE ROLE $REPLICATION_USER WITH REPLICATION LOGIN PASSWORD '$OG_REPLICATOR_PASSWORD';"
fi

# --- 2. Physical replication slot on the primary (idempotent) -----------------------------------------
SLOT_EXISTS="$("${PSQL_PRIMARY[@]}" -tAc "SELECT 1 FROM pg_replication_slots WHERE slot_name='$REPLICATION_SLOT';" 2>/dev/null || echo "")"
if [ "$SLOT_EXISTS" = "1" ]; then
    log "Replication slot '$REPLICATION_SLOT' already exists, skipping."
else
    log "Creating physical replication slot '$REPLICATION_SLOT'."
    run "${PSQL_PRIMARY[@]}" -c "SELECT pg_create_physical_replication_slot('$REPLICATION_SLOT');"
fi

# --- 3. pg_hba.conf reminder (never edited automatically -- a primary auth change is deliberate) -----
log "Reminder: the primary's pg_hba.conf must allow '$REPLICATION_USER' to connect for replication"
log "  from $PRIMARY_HOST (e.g. 'host replication $REPLICATION_USER 127.0.0.1/32 scram-sha-256')."
log "  Not edited by this script; 'systemctl reload postgresql' after adding it by hand if needed."

# --- 4. Base backup into the standby data directory (idempotent: skip if already populated) ----------
if [ -d "$STANDBY_DATA_DIR" ] && [ -n "$(ls -A "$STANDBY_DATA_DIR" 2>/dev/null || true)" ]; then
    log "Standby data dir '$STANDBY_DATA_DIR' is not empty, assuming base backup already taken. Skipping pg_basebackup."
else
    log "Taking base backup into '$STANDBY_DATA_DIR' via pg_basebackup (streaming WAL, standby mode)."
    run mkdir -p "$STANDBY_DATA_DIR"
    run env PGPASSWORD="${OG_REPLICATOR_PASSWORD:-}" pg_basebackup \
        --host="$PRIMARY_HOST" --port="$PRIMARY_PORT" --username="$REPLICATION_USER" \
        --pgdata="$STANDBY_DATA_DIR" \
        --write-recovery-conf \
        --slot="$REPLICATION_SLOT" \
        --checkpoint=fast \
        --wal-method=stream \
        --verbose --progress
    log "Setting standby port to $STANDBY_PORT in postgresql.auto.conf."
    run bash -c "echo \"port = $STANDBY_PORT\" >> '$STANDBY_DATA_DIR/postgresql.auto.conf'"
fi

log "Done. Start the standby with the systemd unit deploy/ha/systemd-dropins/og-postgres-standby.service"
log "(a second postgresql instance pointed at PGDATA=$STANDBY_DATA_DIR, port=$STANDBY_PORT), or manually:"
log "  pg_ctl -D '$STANDBY_DATA_DIR' -l /var/log/postgresql/standby.log start"
log ""
log "Monitoring query (run on the PRIMARY to confirm the standby is streaming and caught up):"
log "  SELECT application_name, client_addr, state, sync_state,"
log "         pg_wal_lsn_diff(pg_current_wal_lsn(), replay_lsn) AS replay_lag_bytes"
log "  FROM pg_stat_replication;"
log "Also check on the PRIMARY: SELECT slot_name, active, pg_wal_lsn_diff(pg_current_wal_lsn(), restart_lsn)"
log "  AS retained_wal_bytes FROM pg_replication_slots WHERE slot_name = '$REPLICATION_SLOT';"
log "  (a growing retained_wal_bytes with active=false means the standby is down and WAL is piling up --"
log "  alert on this, e.g. from opengrid.health, before the primary's disk fills.)"
