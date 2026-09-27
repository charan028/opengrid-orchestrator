#!/bin/bash
# deploy/scripts/topology_backfill.sh -- add ONLY the missing grid-topology rows to an existing database.
#
#   bash deploy/scripts/topology_backfill.sh [--apply] [--release DIR] [--etc DIR]
#                                            [--db-port N] [--db-name NAME] [--db-role ROLE] [--only HUB_ID]
#
# Runs dev/seed/topology_seed.py --only-missing against the database, with the generated sim configs in
# <etc>/sim (the ones the fleet seed used). Insert-only: service transformers for every hub (home banks, the
# substation set, the trucks), og.hub.transformer_id only where it is NULL, missing feeder/substation limits and
# HOME_BANK asset rows, and the POI premise (service_kw/export_limit_kw = nameplate) of the substation set's and
# the trucks' hubs where NULL. It never rewrites an existing og.bank, og.hub, limit, transformer or asset row: the
# script verifies that inside its one transaction and rolls back on any difference, and this wrapper also
# compares the LZ_AEN bank/hub checksums before and after.
#
# Default: DRY RUN (the transaction is rolled back; the plan is printed: rows per statement, unmapped counts
# before -> after). --apply commits. Idempotent: a second --apply inserts 0 rows. The guardian's topology cache
# picks the rows up within its refresh (60 s); no restart is needed. Prints counts only, never a credential.
#
# --only HUB_ID: just that dedicated-connection hub's bank -- e.g. --only sub-LZ_AEN-00, the toll hotfix (its
# 20,408 kVA transformer, the mapping and service_kw = export_limit_kw = 20,000 kW), same guards and dry run.
set -euo pipefail

if [ -z "${OG_BOOT_THROTTLED:-}" ]; then
  export OG_BOOT_THROTTLED=1
  exec ionice -c3 nice -n 19 env TMPDIR=/dev/shm bash "$0" "$@"
fi

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RELEASE="$(cd "$SCRIPT_DIR/../.." && pwd)"
ETC=/etc/opengrid
DB_HOST=127.0.0.1
DB_PORT=5432
DB_NAME=og
DB_ROLE=opengrid
APPLY=0
ONLY=""
OG_PY=/opt/opengrid/venv/bin/python

while [ $# -gt 0 ]; do
  case "$1" in
    --apply) APPLY=1; shift ;;
    --only) ONLY="$2"; shift 2 ;;
    --release) RELEASE="$(cd "$2" && pwd)"; shift 2 ;;
    --etc) ETC="$2"; shift 2 ;;
    --db-port) DB_PORT="$2"; shift 2 ;;
    --db-name) DB_NAME="$2"; shift 2 ;;
    --db-role) DB_ROLE="$2"; shift 2 ;;
    -h|--help) sed -n '2,20p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done

# shellcheck source=deploy/scripts/lib_secrets.sh
. "$SCRIPT_DIR/lib_secrets.sh"
SIM_DIR="$ETC/sim"
[ -f "$SIM_DIR/fleet.yaml" ] && [ -f "$SIM_DIR/scada.yaml" ] || die "sim configs missing in $SIM_DIR"
[ -f "$RELEASE/dev/seed/topology_seed.py" ] || die "release $RELEASE has no dev/seed/topology_seed.py"

# The DB password from <etc>/secrets.env, exported only inside the subshell.
with_db() {
  (
    set +x
    OG_DB_PASSWORD="$(env_get "$ETC/secrets.env" OG_DB_PASSWORD)"
    [ -n "$OG_DB_PASSWORD" ] || die "OG_DB_PASSWORD missing in $ETC/secrets.env"
    export PGPASSWORD="$OG_DB_PASSWORD" PYTHONPATH="$RELEASE/orchestrator/src"
    export PGHOST="$DB_HOST" PGPORT="$DB_PORT" PGUSER="$DB_ROLE" PGDATABASE="$DB_NAME"
    "$@"
  )
}

# LZ_AEN bank and hub checksums (trucks excluded): the columns a backfill must never touch.
aen_checksums() {
  with_db env PGOPTIONS='-c default_transaction_read_only=on' psql -X -qAt -F ' | ' \
    -c "SELECT 'banks', count(*), md5(string_agg(bank_id || zone || kva_rating::text || coalesce(feeder_id, ''), ',' ORDER BY bank_id)) FROM og.bank WHERE zone = 'LZ_AEN' AND bank_id NOT LIKE 'bank-truck-%'" \
    -c "SELECT 'hubs', count(*), md5(string_agg(hub_id || bank_id || zone || p_kw::text || e_kwh::text || units::text, ',' ORDER BY hub_id)) FROM og.hub WHERE zone = 'LZ_AEN' AND bank_id NOT LIKE 'bank-truck-%'"
}

echo "topology backfill: db $DB_NAME on port $DB_PORT, release $RELEASE, $([ "$APPLY" -eq 1 ] && echo APPLY || echo 'DRY RUN')"
before="$(aen_checksums)"
echo "$before" | sed 's/^/  LZ_AEN before: /'
mode=(--dry-run); [ "$APPLY" -eq 1 ] && mode=()
scope=(--only-missing); [ -n "$ONLY" ] && scope=(--only "$ONLY")
rc=0
with_db "$OG_PY" "$RELEASE/dev/seed/topology_seed.py" --fleet-config "$SIM_DIR/fleet.yaml" \
  --scada-config "$SIM_DIR/scada.yaml" "${scope[@]}" "${mode[@]}" \
  --dsn "host=$DB_HOST port=$DB_PORT dbname=$DB_NAME user=$DB_ROLE" | sed 's/^/  /' || rc=$?
after="$(aen_checksums)"
echo "$after" | sed 's/^/  LZ_AEN after:  /'
[ "$before" = "$after" ] || die "LZ_AEN bank/hub checksums changed"
echo "  LZ_AEN checksums identical: OK"
[ "$rc" -eq 0 ] || die "topology_seed.py exited $rc (unmapped rows remain or the guard rolled back)"
echo "topology backfill: done"
