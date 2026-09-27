#!/bin/bash
# deploy/scripts/render_mosquitto_acl.sh OUT [SECRETS_FILE] -- write production's Mosquitto ACL to OUT.
#
# The six core users come from dev/scripts/gen_mosquitto_acl.py (SECRETS_FILE, when given, is its
# --secrets-file cross-check; without it the script checks against dev/secrets.example). Production carries two
# grants beyond those six (wiring note: fold them into gen_mosquitto_acl.py); they are added here, in one place,
# for both bootstrap_from_scratch.sh (phase g) and the Kubernetes broker (deploy/k8s). The ACL holds no secret.
set -euo pipefail

out="${1:?usage: render_mosquitto_acl.sh OUT [SECRETS_FILE]}"
secrets="${2:-}"
release="$(cd "$(dirname "$0")/../.." && pwd)"
root=og/v1

args=(--topic-root "$root" --out "$out")
[ -z "$secrets" ] || args+=(--secrets-file "$secrets")
"${OG_PY:-python3}" "$release/dev/scripts/gen_mosquitto_acl.py" "${args[@]}" >/dev/null
sed -i "/^user og_api\$/a topic write $root/scada/wave/+/+/+/request" "$out"
printf '\nuser og_sim_customer\ntopic write %s/site/#\ntopic write %s/corridor/#\n' "$root" "$root" >> "$out"
