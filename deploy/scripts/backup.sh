#!/bin/bash
# deploy/scripts/backup.sh
#
# Nightly pg_dump of the `og` database to /srv/ogbackup (its own LV, 2026-09-26; override with
# OG_BACKUP_DIR) with 7-day rotation. The directory must be writable by the opengrid user.
# Run as the opengrid user (via /etc/cron.d/opengrid), which already owns/can write
# /var/lib/opengrid; no root needed for the dump itself. og_test and the per-workspace
# og_t_* databases are never backed up (disposable, see BUILD.md §5).
#
# Requires OG_DB_PASSWORD in the environment (sourced from /etc/opengrid/secrets.env by the
# caller) — this script never prints it.
set -euo pipefail

BACKUP_DIR="${OG_BACKUP_DIR:-/srv/ogbackup}"
KEEP_DAYS=7
DB=og
DB_USER=opengrid
DB_HOST=127.0.0.1
DB_PORT=5432

mkdir -p "$BACKUP_DIR"
STAMP="$(date +%F)"
OUT="$BACKUP_DIR/og-$STAMP.dump"
TMP="$OUT.in-progress"

export PGPASSWORD="${OG_DB_PASSWORD:?OG_DB_PASSWORD not set}"
pg_dump --host="$DB_HOST" --port="$DB_PORT" --username="$DB_USER" --format=custom --file="$TMP" "$DB"
mv "$TMP" "$OUT"
chmod 640 "$OUT"

find "$BACKUP_DIR" -maxdepth 1 -name 'og-*.dump' -mtime +"$KEEP_DAYS" -delete

echo "$(date -Is) backup OK: $OUT"
