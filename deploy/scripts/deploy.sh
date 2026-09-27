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
#
# Delivery preflight (policy pending the owner's decision -- warn, never hard-block): restarting
# og-engine/og-guardian/og-sim-* while a FIRM or AS obligation is DELIVERING interrupts it (hubs hold
# their last setpoint only for lease + hold, ~35 s). If any is delivering, the deploy stops with a
# warning unless `--during-delivery` is given, in which case it warns and proceeds.
set -euo pipefail

DURING_DELIVERY=0
if [ "${1:-}" = "--during-delivery" ]; then
  DURING_DELIVERY=1
  shift
fi
RELEASE_SRC="${1:?usage: deploy.sh [--during-delivery] <release_dir>}"
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

ensure_runtime_dirs() {  # directories the og-* units write to (ReadWritePaths); idempotent
  install -d -o opengrid -g opengrid -m 750 /var/lib/opengrid /var/lib/opengrid/anchors /var/log/opengrid
  if [ -d /srv/ogbackup ]; then
    install -d -o opengrid -g opengrid -m 750 /srv/ogbackup/anchors /srv/ogbackup/cold
  else
    echo "$(ts) NOTE: /srv/ogbackup missing; anchors publish to /var/lib/opengrid/anchors only" | tee -a "$LOG"
  fi
}

# Every unit the two targets pull in must be active AND every og-* process must write a heartbeat newer than
# the restart; og-api's /health alone missed a unit that failed to start (review 2026-09-26).
HEARTBEAT_PROCESSES="api engine feeds guardian safestop settle"
post_restart_check() {  # $1 = restart timestamp (ISO); returns non-zero on failure (-> ERR trap -> rollback)
  local since="$1" units unit bad hb
  units="$(systemctl list-dependencies --plain --no-legend opengrid.target ogsim.target 2>/dev/null \
           | awk '{print $1}' | grep -E '^og-.*\.service$' | sort -u | tr '\n' ' ')"
  for _ in $(seq 1 30); do
    bad=""
    for unit in $units; do systemctl is-active --quiet "$unit" || bad="$bad $unit"; done
    hb="$(runuser -u opengrid -- bash -c '
      set -a; . /etc/opengrid/secrets.env; set +a
      PGPASSWORD="$OG_DB_PASSWORD" psql -h 127.0.0.1 -U opengrid -d og -At -c "
        SELECT count(DISTINCT process) FROM og.heartbeat WHERE ts > '"'$since'"'::timestamptz
          AND process = ANY(string_to_array('"'$HEARTBEAT_PROCESSES'"', '"' '"'))"
    ' 2>/dev/null || echo 0)"
    if [ -z "$bad" ] && [ "$hb" = "$(echo $HEARTBEAT_PROCESSES | wc -w)" ]; then
      echo "$(ts) post-restart check OK: $(echo $units | wc -w) units active, $hb/$(echo $HEARTBEAT_PROCESSES | wc -w) heartbeats fresh" | tee -a "$LOG"
      return 0
    fi
    sleep 3
  done
  echo "$(ts) post-restart check FAILED: inactive:${bad:- none}; fresh heartbeats $hb/$(echo $HEARTBEAT_PROCESSES | wc -w)" | tee -a "$LOG"
  return 1
}

install_units() {  # $1 = release dir; drop-ins under /etc/systemd/system/<unit>.d/ are left untouched
  local dir="$1/deploy/systemd"
  [ -d "$dir" ] || return 0
  install -o root -g root -m 644 "$dir"/*.service "$dir"/*.target /etc/systemd/system/
  if compgen -G "$dir/*.timer" >/dev/null; then install -o root -g root -m 644 "$dir"/*.timer /etc/systemd/system/; fi
  systemctl daemon-reload
}

# FIRM and AS services (02a S3.7 priority buckets); ERCOT_ENERGY is market, HOME never delivers here.
DELIVERING="$(runuser -u opengrid -- bash -c '
  set -a; . /etc/opengrid/secrets.env; set +a
  PGPASSWORD="$OG_DB_PASSWORD" psql -h 127.0.0.1 -U opengrid -d og -At -F " " -c "
    SELECT service_type, obligation_id, window_end FROM og.obligation
    WHERE state = '"'"'DELIVERING'"'"'
      AND service_type IN ('"'"'ERCOT_AS'"'"','"'"'DIST_DEFERRAL'"'"','"'"'PARTNER_CAPACITY'"'"','"'"'DATA_CENTER'"'"')"
' 2>/dev/null || echo "UNKNOWN (preflight query failed)")"
if [ -n "$DELIVERING" ]; then
  echo "$(ts) WARNING: FIRM/AS obligations are DELIVERING; a restart interrupts them:" | tee -a "$LOG"
  echo "$DELIVERING" | sed 's/^/    /' | tee -a "$LOG"
  if [ "$DURING_DELIVERY" -ne 1 ]; then
    echo "$(ts) deploy NOT started: re-run with --during-delivery to deploy anyway" | tee -a "$LOG"
    exit 3
  fi
  echo "$(ts) --during-delivery given: proceeding" | tee -a "$LOG"
fi

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
    install_units "$PREV_TARGET" || true
    systemctl restart opengrid.target ogsim.target || true
    echo "$(ts) rolled back to $PREV_TARGET" | tee -a "$LOG"
  else
    echo "$(ts) no previous release to roll back to; leaving current as-is" | tee -a "$LOG"
  fi
  exit 1
}
trap rollback ERR

echo "$(ts) running migrations" | tee -a "$LOG"
# Merge fix: opengrid.platform.db.build_dsn() resolves OG_DB_PASSWORD via resolve_secret(), which
# raises unless the env var is actually set -- it was never being loaded here, so every deploy failed
# migrations before touching the database. secrets.env/api_keys.env are root:opengrid mode 640
# (deploy/README.md), readable by opengrid, so `runuser -u opengrid` can source them directly.
runuser -u opengrid -- bash -c '
  set -a
  . /etc/opengrid/secrets.env
  . /etc/opengrid/api_keys.env
  set +a
  export OG_CONFIG="'"$NEW_RELEASE"'/orchestrator/config/orchestrator.toml"
  export PYTHONPATH="'"$NEW_RELEASE"'/orchestrator/src"
  exec "'"$VENV"'" -m opengrid.platform.db migrate
'

# The seed upserts (ON CONFLICT DO UPDATE) and bank/hub ids follow the ENABLED zone blocks in order, so it
# must read the same fleet config the production simulators run. When /etc/opengrid/sim/fleet.yaml (the
# generated production override, deploy/README.md) exists it wins over the repo yaml: seeding from the repo
# yaml would re-key existing zone banks (e.g. LZ_AEN bank-040..049 rewritten as another zone).
SIM_FLEET_OVERRIDE=/etc/opengrid/sim/fleet.yaml
if [ -f "$SIM_FLEET_OVERRIDE" ]; then
  echo "$(ts) seeding fleet topology (idempotent) from $SIM_FLEET_OVERRIDE" | tee -a "$LOG"
else
  SIM_FLEET_OVERRIDE=""
  echo "$(ts) seeding fleet topology (idempotent) from the release's integration-sims/config/fleet.yaml" | tee -a "$LOG"
fi
runuser -u opengrid -- bash -c '
  set -a
  . /etc/opengrid/secrets.env
  . /etc/opengrid/api_keys.env
  set +a
  if [ -n "'"$SIM_FLEET_OVERRIDE"'" ]; then export OG_FLEET_SIM_CONFIG="'"$SIM_FLEET_OVERRIDE"'"; fi
  export OG_CONFIG="'"$NEW_RELEASE"'/orchestrator/config/orchestrator.toml"
  export PYTHONPATH="'"$NEW_RELEASE"'/orchestrator/src"
  exec "'"$VENV"'" -m opengrid.fleet.seed
'

echo "$(ts) switching current -> $NEW_RELEASE" | tee -a "$LOG"
ln -sfn "$NEW_RELEASE" "$CURRENT"

echo "$(ts) installing systemd units from the release (timers are installed, never enabled here)" | tee -a "$LOG"
install_units "$NEW_RELEASE"

ensure_runtime_dirs
RESTART_AT="$(date -Is)"
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

if ! post_restart_check "$RESTART_AT"; then
  false   # triggers the ERR trap -> rollback
fi

trap - ERR
echo "$(ts) deploy OK: $NEW_RELEASE" | tee -a "$LOG"

# Prune old releases, keep the most recent $KEEP_RELEASES.
cd "$RELEASES"
# shellcheck disable=SC2012
ls -1dt -- */ 2>/dev/null | tail -n +"$((KEEP_RELEASES + 1))" | xargs -r rm -rf --
