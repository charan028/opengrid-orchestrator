#!/bin/bash
# deploy/scripts/create_schema.sh -- create the OpenGrid database from nothing: the role, the database, the
# extensions and the og schema, then every migration 0001..latest through the one runner
# (`python -m opengrid.platform.db migrate`). Idempotent: an existing role, database or schema is kept and only
# pending migrations run. Used by bootstrap_from_scratch.sh phases c and d (deploy/BOOTSTRAP.md).
#
#   bash deploy/scripts/create_schema.sh [options]
#
#   --release DIR       release/repo root (default: this script's repo)
#   --etc DIR           where secrets.env holds OG_DB_PASSWORD (default /etc/opengrid; generated when absent)
#   --db-port N --db-name NAME --db-role ROLE   (default 5432 / og / opengrid)
#   --db-host HOST      server host (default 127.0.0.1)
#   --config FILE       orchestrator.toml of the migrate step (default: the release's; [postgres].host = --db-host)
#   --fresh-db          drop the database first (refused on port 5432)
#   --no-migrate        role, database, extensions and schema only
#   --snapshot FILE     after migrating, write the normalised `pg_dump --schema-only` of schema og to FILE
#   --check-snapshot    after migrating, diff that dump against orchestrator/schema/og_schema.sql (exit 3 on drift)
#   --dry-run           print what would be done
#
# Extensions: none are required. gen_random_uuid() is core since PostgreSQL 13 (the server must be >= 13);
# add any future extension to EXTENSIONS below, never inside a migration (it needs a superuser).
# The role's password never appears in argv or output: it goes to psql over stdin.
# Admin connection: `runuser -u postgres` on the local socket; when OG_PG_ADMIN_PASSWORD is set in the environment
# (never argv), TCP to --db-host as OG_PG_ADMIN_USER (default postgres) instead (the deploy/k8s migrations Job).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=deploy/scripts/lib_secrets.sh
. "$SCRIPT_DIR/lib_secrets.sh"

RELEASE="$(cd "$SCRIPT_DIR/../.." && pwd)"
ETC=/etc/opengrid
DB_HOST=127.0.0.1
CONFIG=""
DB_PORT=5432
DB_NAME=og
DB_ROLE=opengrid
FRESH_DB=0
MIGRATE=1
SNAPSHOT=""
CHECK=0
DRY=0
EXTENSIONS=""
OG_PY="${OG_PY:-/opt/opengrid/venv/bin/python}"
PG_BIN="${PG_BIN:-/usr/lib/postgresql/17/bin}"

while [ $# -gt 0 ]; do
  case "$1" in
    --release) RELEASE="$(cd "$2" && pwd)"; shift 2 ;;
    --etc) ETC="$2"; shift 2 ;;
    --db-port) DB_PORT="$2"; shift 2 ;;
    --db-name) DB_NAME="$2"; shift 2 ;;
    --db-role) DB_ROLE="$2"; shift 2 ;;
    --db-host) DB_HOST="$2"; shift 2 ;;
    --config) CONFIG="$2"; shift 2 ;;
    --fresh-db) FRESH_DB=1; shift ;;
    --no-migrate) MIGRATE=0; shift ;;
    --snapshot) SNAPSHOT="$2"; shift 2 ;;
    --check-snapshot) CHECK=1; shift ;;
    --dry-run) DRY=1; shift ;;
    -h|--help) sed -n '2,24p' "$0"; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
done
SNAPSHOT_REF="$RELEASE/orchestrator/schema/og_schema.sql"
# og_t_* workspace databases exist only on the test cluster (tests/integration/cluster_guard.py is the
# same rule for the Python helpers). Checked before anything else, --dry-run included.
case "$DB_NAME" in
  og_t_*) [ "$DB_PORT" = 5433 ] || die "refusing to create workspace database $DB_NAME on port $DB_PORT: og_t_* databases live only on the test cluster (port 5433)" ;;
esac

pg_admin() {
  if [ -n "${OG_PG_ADMIN_PASSWORD:-}" ]; then
    PGPASSWORD="$OG_PG_ADMIN_PASSWORD" psql -X -q -v ON_ERROR_STOP=1 -h "$DB_HOST" -p "$DB_PORT" \
      -U "${OG_PG_ADMIN_USER:-postgres}" -d postgres "$@"
  else
    runuser -u postgres -- psql -X -q -v ON_ERROR_STOP=1 -p "$DB_PORT" "$@"
  fi
}
count() { pg_admin -Atc "$1"; }

with_db() {  # the orchestrator's DB environment, password exported only inside the subshell
  (
    OG_DB_PASSWORD="$(env_get "$ETC/secrets.env" OG_DB_PASSWORD)"
    [ -n "$OG_DB_PASSWORD" ] || die "OG_DB_PASSWORD missing in $ETC/secrets.env"
    export OG_DB_PASSWORD PGPASSWORD="$OG_DB_PASSWORD"
    export OG_CONFIG="${CONFIG:-$RELEASE/orchestrator/config/orchestrator.toml}" PYTHONPATH="$RELEASE/orchestrator/src"
    export OG_DB="$DB_NAME" OG_DB_PORT="$DB_PORT" OG_DB_USER="$DB_ROLE"
    export PGHOST="$DB_HOST" PGPORT="$DB_PORT" PGUSER="$DB_ROLE" PGDATABASE="$DB_NAME"
    "$@"
  )
}

# The committed snapshot is the schema only: no owner/privileges (the role name differs per host), no
# psql meta lines (\restrict carries a random key per dump), no version banner.
dump_schema() {
  with_db "$PG_BIN/pg_dump" --schema-only --no-owner --no-privileges --no-comments --schema=og \
    | sed -e '/^\\restrict /d' -e '/^\\unrestrict /d' -e '/^-- Dumped from database version/d' \
          -e '/^-- Dumped by pg_dump version/d' \
    | cat -s
}

echo "create_schema: db $DB_NAME on port $DB_PORT as $DB_ROLE (release $RELEASE)$( [ "$DRY" -eq 1 ] && echo ' DRY RUN')"
rc=0
env_ensure "$ETC/secrets.env" OG_DB_PASSWORD || rc=$?
if [ "$DRY" -eq 1 ]; then
  echo "  DRY: role $DB_ROLE, database $DB_NAME (owner $DB_ROLE), schema og, extensions [${EXTENSIONS:-none}]"
  [ "$MIGRATE" -eq 1 ] && echo "  DRY: $OG_PY -m opengrid.platform.db migrate"
  exit 0
fi

ver="$(count 'SHOW server_version_num')"
[ "$ver" -ge 130000 ] || die "PostgreSQL >= 13 required (gen_random_uuid), server is $ver"
if [ "$FRESH_DB" -eq 1 ]; then
  [ "$DB_PORT" != 5432 ] || die "--fresh-db is refused on the production cluster (5432)"
  pg_admin -c "DROP DATABASE IF EXISTS \"$DB_NAME\" WITH (FORCE)" 2>/dev/null
  echo "  database $DB_NAME: dropped (fresh)"
fi
if [ "$(count "SELECT count(*) FROM pg_roles WHERE rolname = '$DB_ROLE'")" = 0 ]; then
  pg_admin -c "CREATE ROLE \"$DB_ROLE\" LOGIN"; echo "  role $DB_ROLE: created"; rc=10
else
  echo "  role $DB_ROLE: present"
fi
# Converge the password to the file every run (over stdin): a no-op when they already match.
printf "ALTER ROLE \"%s\" PASSWORD '%s';\n" "$DB_ROLE" "$(env_get "$ETC/secrets.env" OG_DB_PASSWORD)" | pg_admin -f -
echo "  role $DB_ROLE: password $([ "$rc" -eq 10 ] && echo set || echo synced) from $ETC/secrets.env"
if [ "$(count "SELECT count(*) FROM pg_database WHERE datname = '$DB_NAME'")" = 0 ]; then
  pg_admin -c "CREATE DATABASE \"$DB_NAME\" OWNER \"$DB_ROLE\""; echo "  database $DB_NAME: created"
else
  echo "  database $DB_NAME: present"
fi
for ext in $EXTENSIONS; do
  pg_admin -d "$DB_NAME" -c "CREATE EXTENSION IF NOT EXISTS \"$ext\""; echo "  extension $ext: ok"
done
pg_admin -d "$DB_NAME" -c "CREATE SCHEMA IF NOT EXISTS og AUTHORIZATION \"$DB_ROLE\""
echo "  schema og: ok (owner $(pg_admin -d "$DB_NAME" -Atc "SELECT nspowner::regrole FROM pg_namespace WHERE nspname = 'og'"))"

[ "$MIGRATE" -eq 1 ] || exit 0
with_db "$OG_PY" -m opengrid.platform.db migrate | sed 's/^/  /'
files="$(find "$RELEASE/orchestrator/migrations" -maxdepth 1 -name '[0-9][0-9][0-9][0-9]_*.sql' | wc -l)"
applied="$(with_db psql -X -At -c 'SELECT count(*) FROM og.schema_migrations')"
echo "  migrations: $applied applied / $files in the release"
[ "$applied" -ge "$files" ] || die "not every migration is recorded as applied"

if [ -n "$SNAPSHOT" ]; then
  mkdir -p "$(dirname "$SNAPSHOT")"
  last="$(find "$RELEASE/orchestrator/migrations" -maxdepth 1 -name '[0-9][0-9][0-9][0-9]_*.sql' -printf '%f\n' | sort | tail -1)"
  { printf -- '-- og_schema: GENERATED by deploy/scripts/create_schema.sh --snapshot (make schema-snapshot). Do not edit.\n'
    printf -- '-- og_schema: pg_dump --schema-only of schema og on a fresh database after migrations 0001..%s.\n' "$last"
    printf -- '-- og_schema: `make schema-check` fails when the migrations no longer produce exactly this schema.\n'
    dump_schema; } > "$SNAPSHOT"
  echo "  snapshot: $SNAPSHOT ($(wc -l < "$SNAPSHOT") lines, through $last)"
fi
if [ "$CHECK" -eq 1 ]; then
  [ -f "$SNAPSHOT_REF" ] || die "no committed snapshot at $SNAPSHOT_REF"
  if diff -u <(grep -v '^-- og_schema:' "$SNAPSHOT_REF") <(dump_schema) > /dev/shm/og_schema.diff; then
    echo "  schema snapshot: matches the migrations"
  else
    echo "  schema snapshot: DRIFT against the migrations (first lines; full diff in /dev/shm/og_schema.diff):"
    head -40 /dev/shm/og_schema.diff; exit 3
  fi
fi
