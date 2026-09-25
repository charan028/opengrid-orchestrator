#!/bin/bash
# deploy/scripts/rollback.sh <release_name>
#
# Switches /opt/opengrid/current to an already-deployed release under
# /opt/opengrid/releases/<release_name> (e.g. a timestamp printed by a previous deploy.sh
# run), restarts the targets, and health-checks. Must be run as root. No down-migrations are
# run (forward-only migrations, per 02b-mvp-s-spec-platform.md §9.6); every migration must be
# written to tolerate a rollback to older code running against a slightly-ahead schema.
set -euo pipefail

TARGET="${1:?usage: rollback.sh <release_name_under_releases/>}"
BASE=/opt/opengrid
RELEASES="$BASE/releases"
CURRENT="$BASE/current"
LOG=/var/log/opengrid/deploy.log

if [ "$(id -u)" -ne 0 ]; then
  echo "rollback.sh must be run as root" >&2
  exit 1
fi

DEST="$RELEASES/$TARGET"
if [ ! -d "$DEST" ]; then
  echo "release not found: $DEST" >&2
  exit 1
fi

ts() { date -Is; }
mkdir -p /var/log/opengrid

echo "$(ts) rollback -> $DEST" | tee -a "$LOG"
ln -sfn "$DEST" "$CURRENT"
systemctl restart opengrid.target ogsim.target

ok=0
for _ in $(seq 1 15); do
  if curl -fsS --max-time 2 http://127.0.0.1:8080/og/api/health >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 2
done
if [ "$ok" -ne 1 ]; then
  echo "$(ts) rollback health check FAILED - manual intervention needed" | tee -a "$LOG"
  exit 1
fi
echo "$(ts) rollback OK: $DEST" | tee -a "$LOG"
