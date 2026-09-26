#!/bin/sh
# dev/postgres/init-og-test-db.sh -- runs once, automatically, the first time the `postgres`
# container's data volume is initialized (docker-entrypoint-initdb.d convention: the official
# postgres image only lets POSTGRES_DB create ONE database at container-creation time, and
# WP D0 needs two -- `og` for app data, `og_test` for pytest -- both owned by `opengrid`,
# per BUILD.md WP D0 item 1).
set -e

psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-EOSQL
    SELECT 'CREATE DATABASE og_test OWNER $POSTGRES_USER'
    WHERE NOT EXISTS (SELECT FROM pg_database WHERE datname = 'og_test')\gexec
EOSQL
