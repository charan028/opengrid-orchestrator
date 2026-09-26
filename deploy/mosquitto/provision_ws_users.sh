#!/usr/bin/env bash
# Per-workspace MQTT users (ogw_<ws> -> ogtest/<ws>/# only). Thin wrapper; all logic, safety checks,
# backups and the rollback note are in provision_ws_users.py next to this file.
#   sudo deploy/mosquitto/provision_ws_users.sh --dry-run
#   sudo deploy/mosquitto/provision_ws_users.sh --apply [--workspaces "guard sims"]
# Rollback: cp -p the printed /etc/mosquitto/*.bak.<timestamp> files back, then `systemctl reload mosquitto`.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "must run as root (reads /etc/mosquitto/opengrid.passwd)" >&2
  exit 2
fi
exec python3 "$(dirname "$(readlink -f "$0")")/provision_ws_users.py" "$@"