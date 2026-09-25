#!/bin/bash
# deploy/scripts/deploy.sh <release_dir>
#
# Deploys a release (a directory containing orchestrator/ and integration-sims/, e.g. an
# extracted tagged checkout of the opengrid-orchestrator repo) to
# /opt/opengrid/releases/<timestamp>, runs migrations, atomically switches the `current`
# symlink, restarts the orchestrator + sims targets, health-checks /og/api/health, and rolls
# back automatically on any failure.
#
# Must be run as root (installed at /opt/opengrid/deploy/scripts/deploy.sh so it survives
# across releases). There is no sudo on this host; this script is invoked directly by
# whoever holds the root SSH session, per BUILD.md/02b-mvp-s-spec-platform.md §9.
set -euo pipefail

RELEASE_SRC="${1:?usage: deploy.sh <release_dir>}"
BASE=/opt/opengrid
RELEASES="$BASE/releases"
CURRENT="$BASE/current"
VENV="$BASE/venv/bin/python"
LOG=/var/log/opengrid/deploy.log
KEEP_RELEASES=5

if [ "$(id -u)" -ne 0 ]; then
  echo "deploy.sh must be run as root" >&2
  exit 1
fi
if [ ! -d "$RELEASE_SRC" ]; then
  echo "release dir not found: $RELEASE_SRC" >&2
  exit 1
fi

mkdir -p "$RELEASES" /var/log/opengrid
ts() { date -Is; }

TS="$(date +%Y%m%d%H%M%S)"
NEW_RELEASE="$RELEASES/$TS"
PREV_TARGET=""
if [ -L "$CURRENT" ]; then
  PREV_TARGET="$(readlink -f "$CURRENT")"
fi

echo "$(ts) deploy start: $RELEASE_SRC -> $NEW_RELEASE (previous: ${PREV_TARGET:-none})" | tee -a "$LOG"

mkdir -p "$NEW_RELEASE"
cp -a "$RELEASE_SRC"/. "$NEW_RELEASE"/
chown -R opengrid:opengrid "$NEW_RELEASE"

rollback() {
  echo "$(ts) deploy FAILED, rolling back" | tee -a "$LOG"
  if [ -n "$PREV_TARGET" ]; then
    ln -sfn "$PREV_TARGET" "$CURRENT"
    systemctl restart opengrid.target ogsim.target || true
    echo "$(ts) rolled back to $PREV_TARGET" | tee -a "$LOG"
  else
    echo "$(ts) no previous release to roll back to; leaving current as-is" | tee -a "$LOG"
  fi
  exit 1
}
trap rollback ERR

echo "$(ts) running migrations" | tee -a "$LOG"
runuser -u opengrid -- env \
  OG_CONFIG="$NEW_RELEASE/orchestrator/config/orchestrator.toml" \
  PYTHONPATH="$NEW_RELEASE/orchestrator/src" \
  "$VENV" -m opengrid.platform.db migrate

echo "$(ts) switching current -> $NEW_RELEASE" | tee -a "$LOG"
ln -sfn "$NEW_RELEASE" "$CURRENT"

echo "$(ts) restarting opengrid.target ogsim.target" | tee -a "$LOG"
systemctl restart opengrid.target ogsim.target

echo "$(ts) health check: GET /og/api/health" | tee -a "$LOG"
ok=0
for _ in $(seq 1 15); do
  if curl -fsS --max-time 2 http://127.0.0.1:8080/og/api/health >/dev/null 2>&1; then
    ok=1
    break
  fi
  sleep 2
done
if [ "$ok" -ne 1 ]; then
  echo "$(ts) health check failed" | tee -a "$LOG"
  false   # triggers the ERR trap -> rollback
fi

trap - ERR
echo "$(ts) deploy OK: $NEW_RELEASE" | tee -a "$LOG"

# Prune old releases, keep the most recent $KEEP_RELEASES.
cd "$RELEASES"
# shellcheck disable=SC2012
ls -1dt -- */ 2>/dev/null | tail -n +"$((KEEP_RELEASES + 1))" | xargs -r rm -rf --
