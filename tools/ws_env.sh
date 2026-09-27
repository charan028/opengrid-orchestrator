# shellcheck shell=bash
# Workspace run environment for tools/remote.ps1 (sourced on the server, then `og_ws_env <ws>` is called).
#
# MQTT credentials: a workspace run gets ONLY its own broker user, `ogw_<ws>` (reaches ogtest/<ws>/# and
# nothing else), from /opt/opengrid/work/<ws>/.mqtt.env (deploy/mosquitto/provision_ws_users.py). Every
# production MQTT credential (OG_MQTT_*) is filtered out of secrets.env and unset if inherited. The per-role
# password variables the processes look up (OG_MQTT_<ROLE>_PASSWORD) are set to a placeholder that is not a
# credential, because opengrid.platform.mqtt / ogsim.common.config use OG_MQTT_WS_USER/OG_MQTT_WS_PASSWORD
# whenever they are set, and refuse to start in a workspace without them.
#
# Other secrets.env/api_keys.env values (DB password, API keys) are still loaded as before.
#
# Usage: og_ws_env <ws> [secrets_file] [api_keys_file] [work_dir]

OG_WS_MQTT_ROLES="ENGINE GUARDIAN SIM API SAFESTOP SIMCTL"
OG_WS_MQTT_PLACEHOLDER="workspace-uses-OG_MQTT_WS_PASSWORD"

og_ws_env() {
  local ws="$1"
  local secrets="${2:-/etc/opengrid/secrets.env}"
  local api_keys="${3:-/etc/opengrid/api_keys.env}"
  local work="${4:-/opt/opengrid/work}"
  local var role

  for var in $(compgen -v | grep '^OG_MQTT_' || true); do
    unset "$var"
  done

  set -a
  # shellcheck disable=SC1090
  . <(grep -vE '^[[:space:]]*(export[[:space:]]+)?OG_MQTT_' "$secrets")
  # shellcheck disable=SC1090
  . <(grep -vE '^[[:space:]]*(export[[:space:]]+)?OG_MQTT_' "$api_keys")
  if [ -f "$work/$ws/.mqtt.env" ]; then
    # shellcheck disable=SC1090
    . <(grep -E '^OG_MQTT_WS_(USER|PASSWORD)=' "$work/$ws/.mqtt.env")
  fi
  set +a

  if [ -n "${OG_MQTT_WS_USER:-}" ]; then
    for role in $OG_WS_MQTT_ROLES; do
      export "OG_MQTT_${role}_PASSWORD=$OG_WS_MQTT_PLACEHOLDER"
    done
  else
    echo "warning: no $work/$ws/.mqtt.env -- this workspace has no MQTT user; MQTT clients will refuse to start" >&2
  fi

  # og_t_* databases exist only on the test cluster: an override to any other port (5432 above all) is
  # refused before OG_DB is exported, so no caller can create or migrate a workspace DB elsewhere.
  if [ "${OG_DB_PORT_OVERRIDE:-5433}" != 5433 ]; then
    echo "og_ws_env: refusing OG_DB_PORT_OVERRIDE=${OG_DB_PORT_OVERRIDE}: og_t_$ws lives only on the test cluster (port 5433)" >&2
    return 1
  fi

  # Workspace DBs live on the separate disposable test cluster (port 5433, fsync off, /srv/pgstandby),
  # never on the production cluster (5432): R2 incident 2026-09-26, test I/O starved production commits.
  export OG_WS="$ws" OG_DB="og_t_$ws" OG_DB_PORT="${OG_DB_PORT_OVERRIDE:-5433}" OG_MQTT_ROOT="ogtest/$ws" \
    PGPORT="${OG_DB_PORT_OVERRIDE:-5433}" PYTHONDONTWRITEBYTECODE=1
}
