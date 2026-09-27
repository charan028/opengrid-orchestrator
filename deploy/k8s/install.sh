#!/usr/bin/env bash
# deploy/k8s/install.sh -- install the complete OpenGrid solution on a new Kubernetes cluster, from scratch.
# Read deploy/k8s/README.md first. Everything converges: re-running any phase is safe.
#
#   deploy/k8s/install.sh --registry REG --host FQDN --api-keys-file FILE [options]
#
# Phases (default: prereqs,images,namespace,secrets,deploy,verify; select with --phase a,b,...):
#   prereqs    list and check every prerequisite (tools, cluster, version, StorageClass, IngressClass,
#              capacity, registry, owner files); prints a PASS/FAIL table and stops on any FAIL      read-only
#   images     build the orchestrator and sims images (deploy/k8s/images) and push them
#   namespace  the namespace (and the image pull Secret from --pull-secret-file)
#   secrets    DB, MQTT, proxy and UI-account secrets generated with openssl, signing keys from the
#              orchestrator's keygen CLIs, the owner's ERCOT/EIA (and optional Anthropic) keys; never printed,
#              never rotated once present
#   deploy     helm upgrade --install of deploy/k8s/chart: Postgres, Mosquitto (production ACL), the migrations and
#              seed Jobs, og-* Deployments with probes and limits, the simulators, the Ingress (basic auth in the
#              gateway), the lifecycle CronJob (suspended); waits for everything, Jobs included
#   verify     rollouts, Jobs, bootstrap_check.py --live counts, heartbeats and health endpoints
# Extra phases (never in the default set):
#   migrate    re-run the migrations Job          seed       re-run the seed Job
#   uninstall  helm uninstall; keeps the Secrets and PVCs unless --purge --yes (deletes the namespace)
#
# Options:
#   --phase LIST            comma-separated phases (see above)
#   --dry-run               read-only: checks run, every change is printed instead; `deploy` renders the chart
#                           with helm template into --render-dir
#   --namespace NS          default opengrid              --release NAME   default opengrid
#   --registry REG          image registry prefix, e.g. registry.example.com/opengrid (required for images)
#   --tag TAG               image tag (default: git short sha of this checkout; a dirty tree is refused)
#   --engine docker|podman  container engine (default: whichever is installed)
#   --skip-build            do not build; the images for --tag must already be in --registry
#   --no-push               build only (for a registry the cluster reads from the local engine, e.g. kind/k3d)
#   --host FQDN             public host name of the UI/API (Ingress rule and CSRF allowed host) (required)
#   --ingress-class NAME    IngressClass (default: the cluster default)
#   --tls-secret NAME       existing kubernetes.io/tls Secret for --host
#   --storage-class NAME    StorageClass for the PVCs (default: the cluster default)
#   --external-db HOST[:PORT]  use an existing Postgres 17 (role and database created beforehand)
#   --db-password-file F    the external database role's password (first line), with --external-db
#   --api-keys-file F       the owner's api_keys.env (ERCOT_*, EIA_API_KEY); required unless --no-api-keys
#   --no-api-keys           install without live-feed keys (og-feeds runs degraded; the market simulator stays)
#   --anthropic-key-file F  the owner's ai_agent.env (ANTHROPIC_API_KEY); optional
#   --pull-secret-file F    a .dockerconfigjson for the registry (creates Secret og-pull)
#   --zones LIST            enabled zone blocks (default LZ_AEN)
#   --noie-blocks           also LZ_LCRA,LZ_RAYBN: regulated NOIE, UNAVAILABLE, no contract (D-37; --d32 is an alias)
#   --grid-link-config F    enable the D-34 grid link with this [grid_link] override (loopback by default; README)
#   --grid-link-certs DIR   the TLS files the override names under /etc/opengrid/certs (with --grid-link-config)
#   --demo-customers        verify: run dev/scripts/seed_demo_customers.py once the API is up
#   --values FILE           extra helm values file (repeatable)
#   --timeout DUR           helm/rollout wait (default 30m)
#   --render-dir DIR        where --dry-run writes the rendered manifests (default: a temp dir, printed)
#   --purge --yes           with uninstall: delete the namespace, its Secrets and all data (irreversible)
#
# Nothing prints a secret: values are generated into a private temp dir (removed on exit) and sent to the API
# with `kubectl create`/`patch --patch-file`, never `apply` (which would copy them into an annotation).
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/../.." && pwd)"
CHART="$SCRIPT_DIR/chart"
TOML="$REPO/orchestrator/config/orchestrator.toml"
APACHE_CONF="$REPO/deploy/apache/opengrid.conf"

PHASES="prereqs,images,namespace,secrets,deploy,verify"
DRY=0
NS=opengrid
REL=opengrid
REGISTRY=""
TAG=""
ENGINE=""
SKIP_BUILD=0
NO_PUSH=0
HOST=""
INGRESS_CLASS=""
TLS_SECRET=""
STORAGE_CLASS=""
EXTERNAL_DB=""
DB_PASSWORD_FILE=""
API_KEYS_FILE=""
NO_API_KEYS=0
ANTHROPIC_FILE=""
PULL_SECRET_FILE=""
GRID_LINK_CONFIG=""
GRID_LINK_CERTS=""
ZONES="LZ_AEN"
DEMO=0
VALUES_FILES=()
TIMEOUT=30m
RENDER_DIR=""
PURGE=0
YES=0

MIN_K8S_MINOR=30         # Kubernetes >= 1.30 (Chart.yaml kubeVersion)
MIN_HELM="3.14"
MIN_NODE_EPHEMERAL_GI=20 # images (~1.5 GiB) + emptyDirs + logs, per node
REQUIRED_OWNER_KEYS="ERCOT_PUBLIC_API_KEY_PRIMARY EIA_API_KEY"
UI_BASE_USERS="operator viewer tester"   # deploy/scripts/install.sh

usage() { sed -n '2,53p' "$0"; }

while [ $# -gt 0 ]; do
  case "$1" in
    --phase) PHASES="$2"; shift 2 ;;
    --dry-run) DRY=1; shift ;;
    --namespace) NS="$2"; shift 2 ;;
    --release) REL="$2"; shift 2 ;;
    --registry) REGISTRY="${2%/}"; shift 2 ;;
    --tag) TAG="$2"; shift 2 ;;
    --engine) ENGINE="$2"; shift 2 ;;
    --skip-build) SKIP_BUILD=1; shift ;;
    --no-push) NO_PUSH=1; shift ;;
    --host) HOST="$2"; shift 2 ;;
    --ingress-class) INGRESS_CLASS="$2"; shift 2 ;;
    --tls-secret) TLS_SECRET="$2"; shift 2 ;;
    --storage-class) STORAGE_CLASS="$2"; shift 2 ;;
    --external-db) EXTERNAL_DB="$2"; shift 2 ;;
    --db-password-file) DB_PASSWORD_FILE="$2"; shift 2 ;;
    --api-keys-file) API_KEYS_FILE="$2"; shift 2 ;;
    --no-api-keys) NO_API_KEYS=1; shift ;;
    --anthropic-key-file) ANTHROPIC_FILE="$2"; shift 2 ;;
    --pull-secret-file) PULL_SECRET_FILE="$2"; shift 2 ;;
    --zones) ZONES="$2"; shift 2 ;;
    --noie-blocks|--d32) ZONES="$ZONES,LZ_LCRA,LZ_RAYBN"; shift ;;
    --grid-link-config) GRID_LINK_CONFIG="$2"; shift 2 ;;
    --grid-link-certs) GRID_LINK_CERTS="$2"; shift 2 ;;
    --demo-customers) DEMO=1; shift ;;
    --values) VALUES_FILES+=("$2"); shift 2 ;;
    --timeout) TIMEOUT="$2"; shift 2 ;;
    --render-dir) RENDER_DIR="$2"; shift 2 ;;
    --purge) PURGE=1; shift ;;
    --yes) YES=1; shift ;;
    -h|--help) usage; exit 0 ;;
    *) echo "unknown option: $1 (see --help)" >&2; exit 2 ;;
  esac
done

# --- helpers -----------------------------------------------------------------------------------------------

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
phase() { printf '\n== %s ==\n' "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
want() { [[ ",$PHASES," == *",$1,"* ]]; }
# run CMD...: execute, or print it under --dry-run (never used for a command that carries a secret in argv).
run() { if [ "$DRY" -eq 1 ]; then echo "  DRY: $*"; else "$@"; fi; }
k() { kubectl --request-timeout=30s "$@"; }
kn() { k -n "$NS" "$@"; }

WORK="$(mktemp -d)"
chmod 700 "$WORK"
trap 'rm -rf "$WORK"' EXIT

have() { command -v "$1" >/dev/null 2>&1; }

version_ge() {  # version_ge 3.16.2 3.14 -> true
  [ "$(printf '%s\n%s\n' "$2" "$1" | sort -V | head -1)" = "$2" ]
}

image_ref() {  # orchestrator|sims
  if [ -n "$REGISTRY" ]; then echo "$REGISTRY/opengrid-$1:$TAG"; else echo "opengrid-$1:$TAG"; fi
}

resolve_tag() {
  [ -n "$TAG" ] && return 0
  if ! have git || ! git -C "$REPO" rev-parse --git-dir >/dev/null 2>&1; then die "no git checkout: pass --tag"; fi
  if [ -n "$(git -C "$REPO" status --porcelain --untracked-files=no)" ]; then
    die "the checkout has uncommitted changes: commit them or pass an explicit --tag"
  fi
  TAG="$(git -C "$REPO" rev-parse --short=12 HEAD)"
}

detect_engine() {
  [ -n "$ENGINE" ] && return 0
  if have docker; then ENGINE=docker; elif have podman; then ENGINE=podman; fi
}

# TOML readers for orchestrator.toml (no Python needed on the operator's machine).
toml_section_keys() {  # file section -> the keys of that table, one per line
  awk -v sec="[$2]" '
    { line = $0; sub(/[ \t]*#.*/, "", line) }
    line == sec { inside = 1; next }
    line ~ /^[ \t]*\[/ { inside = 0 }
    inside && line ~ /^[ \t]*"?[A-Za-z0-9_.-]+"?[ \t]*=/ { key = line; sub(/[ \t]*=.*/, "", key); gsub(/[ \t"]/, "", key); print key }
  ' "$1"
}
toml_string_array() {  # file section key -> the array's strings, one per line (single-line arrays)
  awk -v sec="[$2]" -v key="$3" '
    $0 == sec { inside = 1; next }
    /^[ \t]*\[/ { inside = 0 }
    inside && $0 ~ "^[ \\t]*" key "[ \\t]*=" { s = $0; sub(/^[^[]*\[/, "", s); sub(/\].*/, "", s); n = split(s, a, ",")
      for (i = 1; i <= n; i++) { v = a[i]; gsub(/[ \t"]/, "", v); if (v != "") print v } }
  ' "$1"
}
customer_users() { toml_section_keys "$TOML" api.roles.customer; }
ui_users() {
  { for u in $UI_BASE_USERS; do echo "$u"; done
    toml_string_array "$TOML" guardian stop_release_authorised_operators
    customer_users
    toml_section_keys "$TOML" api.roles.utility; } | awk '!seen[$0]++'   # og-util-* (r3.4.3 utility API)
}

# env-file reader: KEY=VALUE lines (systemd EnvironmentFile style, optional surrounding quotes).
env_file_has() {  # file key -> true when the key has a non-empty value
  local v
  v="$(sed -n "s/^[[:space:]]*$2=//p" "$1" | tail -1 | sed -e "s/^['\"]//" -e "s/['\"]\$//")"
  [ -n "$v" ]
}

# Kubernetes resource quantities -> numbers (cpu cores, bytes).
AWK_QTY='
function cpu(q) { if (q == "") return 0; if (q ~ /m$/) return substr(q, 1, length(q) - 1) / 1000; return q + 0 }
function mem(q,   n, u) {
  if (q == "") return 0
  n = q; u = ""
  if (match(q, /[A-Za-z]+$/)) { u = substr(q, RSTART); n = substr(q, 1, RSTART - 1) }
  if (u == "Ki") return n * 1024; if (u == "Mi") return n * 1048576; if (u == "Gi") return n * 1073741824
  if (u == "Ti") return n * 1099511627776; if (u == "k" || u == "K") return n * 1000; if (u == "M") return n * 1e6
  if (u == "G") return n * 1e9; if (u == "T") return n * 1e12; if (u == "m") return n / 1000
  return n + 0
}'

# --- helm arguments (one place for deploy, dry-run rendering and the capacity check) ---------------------------

HELM_SET=()
build_helm_set() {
  local users json="" u host port
  HELM_SET=(--namespace "$NS"
    --set-string "image.registry=$REGISTRY" --set-string "image.tag=$TAG"
    --set-string "ingress.host=$HOST" --set-string "zones=${ZONES//,/\\,}"
    --set-file "gateway.apacheConf=$APACHE_CONF")
  users="$(customer_users)"
  for u in $users; do json+="${json:+,}\"$u\""; done
  HELM_SET+=(--set-json "customerUsers=[$json]")
  [ -z "$INGRESS_CLASS" ] || HELM_SET+=(--set-string "ingress.className=$INGRESS_CLASS")
  [ -z "$TLS_SECRET" ] || HELM_SET+=(--set-string "ingress.tlsSecret=$TLS_SECRET")
  if [ -n "$STORAGE_CLASS" ]; then
    HELM_SET+=(--set-string "postgres.storage.storageClass=$STORAGE_CLASS"
      --set-string "services.settle.persistence.storageClass=$STORAGE_CLASS")
  fi
  if [ -n "$EXTERNAL_DB" ]; then
    host="${EXTERNAL_DB%%:*}"; port=5432
    [[ "$EXTERNAL_DB" == *:* ]] && port="${EXTERNAL_DB##*:}"
    HELM_SET+=(--set postgres.external.enabled=true --set-string "postgres.external.host=$host"
      --set "postgres.external.port=$port")
  fi
  [ -z "$PULL_SECRET_FILE" ] || HELM_SET+=(--set "image.pullSecrets={og-pull}")
  [ -z "$GRID_LINK_CONFIG" ] || HELM_SET+=(--set gridLink.enabled=true)
  local f
  for f in "${VALUES_FILES[@]+"${VALUES_FILES[@]}"}"; do HELM_SET+=(-f "$f"); done
}

render_chart() {  # -> path of the rendered manifests
  local out="$1"
  helm template "$REL" "$CHART" "${HELM_SET[@]}" > "$out"
}

# --- phase: prereqs --------------------------------------------------------------------------------------------

ROWS=()
FAILS=0
row() {  # status check detail
  ROWS+=("$1|$2|$3")
  [ "$1" != FAIL ] || FAILS=$((FAILS + 1))
}
# check STATUS_IF_BAD CHECK GOOD_DETAIL BAD_DETAIL CMD...: PASS when CMD succeeds, else STATUS_IF_BAD (FAIL/WARN).
check() {
  local bad="$1" name="$2" good_detail="$3" bad_detail="$4"
  shift 4
  if "$@"; then row PASS "$name" "$good_detail"; else row "$bad" "$name" "$bad_detail"; fi
}
in_first_column() { printf '%s\n' "$2" | awk '{print $1}' | grep -qx -- "$1"; }
ge() { awk -v a="$1" -v b="$2" 'BEGIN { exit !(a >= b) }'; }
print_table() {
  local r s c d
  printf '\n  %-6s %-30s %s\n' RESULT CHECK DETAIL
  printf '  %-6s %-30s %s\n' ------ ----------------------------- ------------------------------------------
  for r in "${ROWS[@]}"; do
    IFS='|' read -r s c d <<<"$r"
    printf '  %-6s %-30s %s\n' "$s" "$c" "$d"
  done
}

phase_prereqs() {
  phase "prereqs: listing and checking prerequisites"
  local v cluster=0 need_engine=0 sc ic def nodes n_ready alloc

  # Local tools
  if have kubectl; then
    v="$(kubectl version --client -o json 2>/dev/null | sed -n 's/.*"gitVersion": *"\([^"]*\)".*/\1/p' | head -1)"
    row PASS "kubectl" "${v:-present}"
  else row FAIL "kubectl" "not found (https://kubernetes.io/docs/tasks/tools/)"; fi
  if have helm; then
    v="$(helm version --template '{{.Version}}' 2>/dev/null || true)"
    if version_ge "${v#v}" "$MIN_HELM"; then row PASS "helm" "$v (>= $MIN_HELM)"
    else row FAIL "helm" "${v:-unknown} (need >= $MIN_HELM; v3 or v4)"; fi
  else row FAIL "helm" "not found (https://helm.sh/docs/intro/install/)"; fi
  check FAIL "openssl" "$(openssl version 2>/dev/null | cut -d' ' -f1-2)" "not found (secret generation)" have openssl
  for v in awk sed sort tar mktemp; do have "$v" || row FAIL "$v" "not found"; done
  check WARN "curl" "present" "not found: the registry and ingress checks are skipped" have curl

  # Release tree
  local n_mig
  n_mig="$(find "$REPO/orchestrator/migrations" -maxdepth 1 -name '[0-9][0-9][0-9][0-9]_*.sql' 2>/dev/null | wc -l)"
  if [ "$n_mig" -gt 0 ] && [ -f "$TOML" ] && [ -f "$APACHE_CONF" ] && [ -d "$REPO/dev/seed" ] \
      && [ -f "$REPO/deploy/scripts/bootstrap_from_scratch.sh" ] && [ -f "$CHART/Chart.yaml" ]; then
    row PASS "release tree" "$REPO ($n_mig migrations, $(customer_users | wc -l) customer accounts)"
  else row FAIL "release tree" "$REPO is incomplete (migrations, orchestrator.toml, apache conf, seeds, chart)"; fi

  # Inputs
  check FAIL "ingress host (--host)" "$HOST" "required" test -n "$HOST"
  if [ -n "$API_KEYS_FILE" ]; then
    if [ -r "$API_KEYS_FILE" ]; then
      local missing=""
      for v in $REQUIRED_OWNER_KEYS; do env_file_has "$API_KEYS_FILE" "$v" || missing+=" $v"; done
      check FAIL "owner keys (ERCOT/EIA)" "$API_KEYS_FILE: $REQUIRED_OWNER_KEYS present" \
        "$API_KEYS_FILE lacks:$missing" test -z "$missing"
    else row FAIL "owner keys (ERCOT/EIA)" "$API_KEYS_FILE not readable"; fi
  elif [ "$NO_API_KEYS" -eq 1 ]; then row WARN "owner keys (ERCOT/EIA)" "--no-api-keys: og-feeds runs without live data"
  else row FAIL "owner keys (ERCOT/EIA)" "--api-keys-file required (or --no-api-keys)"; fi
  if [ -n "$ANTHROPIC_FILE" ]; then
    if [ -r "$ANTHROPIC_FILE" ] && env_file_has "$ANTHROPIC_FILE" ANTHROPIC_API_KEY; then
      row PASS "owner key (Anthropic)" "$ANTHROPIC_FILE: ANTHROPIC_API_KEY present"
    else row FAIL "owner key (Anthropic)" "$ANTHROPIC_FILE unreadable or without ANTHROPIC_API_KEY"; fi
  else row WARN "owner key (Anthropic)" "none: the copilot runs its no-model tier (optional)"; fi
  if [ -n "$EXTERNAL_DB" ]; then
    check FAIL "external database" "$EXTERNAL_DB (password file readable)" \
      "--db-password-file required and readable with --external-db" test -r "$DB_PASSWORD_FILE"
  fi
  if [ -n "$GRID_LINK_CONFIG" ]; then
    if [ -r "$GRID_LINK_CONFIG" ] && [ -d "$GRID_LINK_CERTS" ] && grep -q '^\[grid_link\]' "$GRID_LINK_CONFIG"; then
      row PASS "grid link (D-34)" "$GRID_LINK_CONFIG + $(find "$GRID_LINK_CERTS" -maxdepth 1 -type f | wc -l) file(s) in $GRID_LINK_CERTS"
    else row FAIL "grid link (D-34)" "--grid-link-config needs a readable [grid_link] file and --grid-link-certs DIR"; fi
  else row PASS "grid link (D-34)" "off (default, as in production)"; fi

  # Cluster
  if have kubectl && k get --raw=/readyz >/dev/null 2>&1; then
    cluster=1
    row PASS "cluster reachable" "$(kubectl config current-context 2>/dev/null || echo '?')"
  else row FAIL "cluster reachable" "kubectl cannot reach the API server (kubeconfig/context?)"; fi

  if [ "$cluster" -eq 1 ]; then
    v="$(k version -o json 2>/dev/null | tr -d '\n ' | sed -n 's/.*"serverVersion":{[^}]*"gitVersion":"\([^"]*\)".*/\1/p')"
    local minor="${v#v1.}"; minor="${minor%%.*}"
    if [ -n "$v" ] && [ "${minor//[^0-9]/}" -ge "$MIN_K8S_MINOR" ] 2>/dev/null; then
      row PASS "kubernetes version" "$v (>= 1.$MIN_K8S_MINOR)"
    else row FAIL "kubernetes version" "${v:-unknown} (need >= 1.$MIN_K8S_MINOR)"; fi

    local denied="" res
    for res in deployments.apps statefulsets.apps jobs.batch cronjobs.batch secrets configmaps services \
        persistentvolumeclaims ingresses.networking.k8s.io networkpolicies.networking.k8s.io; do
      [ "$(k auth can-i create "$res" -n "$NS" 2>/dev/null)" = yes ] || denied+=" $res"
    done
    if ! k get namespace "$NS" >/dev/null 2>&1 && [ "$(k auth can-i create namespaces 2>/dev/null)" != yes ]; then
      denied+=" namespaces"
    fi
    check FAIL "RBAC" "may create every resource kind in namespace $NS" "may not create:$denied" test -z "$denied"

    sc="$(k get storageclass -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.annotations.storageclass\.kubernetes\.io/is-default-class}{"\n"}{end}' 2>/dev/null || true)"
    if [ -n "$STORAGE_CLASS" ]; then
      check FAIL "StorageClass" "$STORAGE_CLASS (--storage-class)" "$STORAGE_CLASS not found" \
        in_first_column "$STORAGE_CLASS" "$sc"
    else
      def="$(echo "$sc" | awk '$2 == "true" {print $1}' | head -1)"
      check FAIL "default StorageClass" "$def" \
        "none marked default (available: $(echo "$sc" | awk '{print $1}' | xargs); pass --storage-class)" test -n "$def"
    fi

    ic="$(k get ingressclass -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.metadata.annotations.ingressclass\.kubernetes\.io/is-default-class}{"\n"}{end}' 2>/dev/null || true)"
    if [ -n "$INGRESS_CLASS" ]; then
      check FAIL "ingress controller" "IngressClass $INGRESS_CLASS" "IngressClass $INGRESS_CLASS not found" \
        in_first_column "$INGRESS_CLASS" "$ic"
    else
      def="$(echo "$ic" | awk '$2 == "true" {print $1}' | head -1)"
      if [ -n "$def" ]; then row PASS "ingress controller" "default IngressClass $def"
      elif [ -n "$(echo "$ic" | xargs)" ]; then row FAIL "ingress controller" "no default IngressClass: pass --ingress-class ($(echo "$ic" | awk '{print $1}' | xargs))"
      else row FAIL "ingress controller" "no IngressClass: install an ingress controller first"; fi
    fi

    # Capacity: free = allocatable of Ready, schedulable nodes - requests of running pods outside this namespace.
    nodes="$(k get nodes -o jsonpath='{range .items[*]}{.metadata.name}{"\t"}{.status.allocatable.cpu}{"\t"}{.status.allocatable.memory}{"\t"}{.status.allocatable.ephemeral-storage}{"\t"}{range .status.conditions[?(@.type=="Ready")]}{.status}{end}{"\t"}{.spec.unschedulable}{"\n"}{end}' 2>/dev/null || true)"
    n_ready="$(echo "$nodes" | awk -F'\t' '$5 == "True" && $6 != "true"' | grep -c . || true)"
    check FAIL "schedulable nodes" "$n_ready Ready" "none Ready and schedulable" test "$n_ready" -gt 0
    alloc="$(echo "$nodes" | awk -F'\t' "$AWK_QTY"'
      $5 == "True" && $6 != "true" { c += cpu($2); m += mem($3); e = mem($4); if (e > me) me = e; if (mem($3) > mm) mm = mem($3) }
      END { printf "%.3f %.0f %.0f %.0f\n", c, m, me, mm }')"
    local used need
    used="$(k get pods -A --field-selector=status.phase!=Succeeded,status.phase!=Failed -o jsonpath='{range .items[*]}{.metadata.namespace}{"\t"}{range .spec.containers[*]}{.resources.requests.cpu}{","}{.resources.requests.memory}{";"}{end}{"\n"}{end}' 2>/dev/null \
      | awk -F'\t' -v ns="$NS" "$AWK_QTY"'
        $1 != ns { n = split($2, cs, ";"); for (i = 1; i <= n; i++) if (cs[i] != "") { split(cs[i], r, ","); c += cpu(r[1]); m += mem(r[2]) } }
        END { printf "%.3f %.0f\n", c, m }')"
    if have helm && [ -n "$HOST" ]; then
      TAG="${TAG:-prereq}"; build_helm_set
      if render_chart "$WORK/prereq.yaml" 2>"$WORK/prereq.err"; then
        need="$(awk "$AWK_QTY"'
          /^[ \t]*requests:[ \t]*\{/ { s = $0; if (match(s, /cpu: *[0-9.]+m?/)) { t = substr(s, RSTART, RLENGTH); sub(/cpu: */, "", t); c += cpu(t) }
                                        if (match(s, /memory: *[0-9.]+[A-Za-z]*/)) { t = substr(s, RSTART, RLENGTH); sub(/memory: */, "", t); m += mem(t) }; next }
          /^[ \t]*requests:[ \t]*$/ { inreq = 1; ind = match($0, /[^ ]/); next }
          inreq { i = match($0, /[^ ]/); if (i <= ind) inreq = 0
                  else { if ($1 == "cpu:") c += cpu($2); if ($1 == "memory:") m += mem($2); if ($1 == "storage:") d += mem($2) } }
          END { printf "%.3f %.0f %.0f\n", c, m, d }' "$WORK/prereq.yaml")"
      else
        row FAIL "chart renders" "$(tail -1 "$WORK/prereq.err")"
      fi
      [ "$TAG" != prereq ] || TAG=""
    fi
    if [ -n "${need:-}" ]; then
      local a_cpu a_mem a_eph u_cpu u_mem n_cpu n_mem n_disk f_cpu f_mem
      read -r a_cpu a_mem a_eph _ <<<"$alloc"
      read -r u_cpu u_mem <<<"$used"
      read -r n_cpu n_mem n_disk <<<"$need"
      f_cpu="$(awk -v a="$a_cpu" -v u="$u_cpu" 'BEGIN { printf "%.1f", a - u }')"
      f_mem="$(awk -v a="$a_mem" -v u="$u_mem" 'BEGIN { printf "%.1f", (a - u) / 2^30 }')"
      n_cpu="$(printf '%.1f' "$n_cpu")"
      n_mem="$(awk -v n="$n_mem" 'BEGIN { printf "%.1f", n / 2^30 }')"
      a_eph="$(awk -v e="$a_eph" 'BEGIN { printf "%.0f", e / 2^30 }')"
      n_disk="$(awk -v d="$n_disk" 'BEGIN { printf "%.0f", d / 2^30 }')"
      check FAIL "cluster CPU" "$n_cpu cores requested; $f_cpu free" "$n_cpu cores requested; only $f_cpu free" \
        ge "$f_cpu" "$n_cpu"
      check FAIL "cluster memory" "$n_mem GiB requested; $f_mem GiB free" "$n_mem GiB requested; only $f_mem GiB free" \
        ge "$f_mem" "$n_mem"
      check FAIL "node disk (ephemeral)" "largest node $a_eph GiB (>= $MIN_NODE_EPHEMERAL_GI); PVCs request $n_disk GiB" \
        "largest node $a_eph GiB (need >= $MIN_NODE_EPHEMERAL_GI)" ge "$a_eph" "$MIN_NODE_EPHEMERAL_GI"
    else
      row FAIL "cluster capacity" "could not compute the chart's requests (helm/--host missing?)"
    fi
  fi

  # Images, registry and container engine
  detect_engine
  if want images && [ "$SKIP_BUILD" -eq 0 ]; then need_engine=1; fi
  if want secrets && { [ "$cluster" -eq 0 ] || ! kn get secret og-signing-keys >/dev/null 2>&1; }; then need_engine=1; fi
  if [ -n "$ENGINE" ] && have "$ENGINE"; then row PASS "container engine" "$ENGINE $("$ENGINE" --version 2>/dev/null | head -1 | awk '{print $3}' | tr -d ,)"
  elif [ "$need_engine" -eq 1 ]; then row FAIL "container engine" "docker or podman needed (image build, signing-key generation)"
  else row PASS "container engine" "not needed for the selected phases"; fi
  if [ -z "$REGISTRY" ]; then
    if want images && [ "$NO_PUSH" -eq 0 ]; then row FAIL "container registry" "--registry required"
    else row WARN "container registry" "none: images must already be on every node (--no-push)"; fi
  elif have curl; then
    local code
    code="$(curl -s -o /dev/null -w '%{http_code}' --max-time 10 "https://${REGISTRY%%/*}/v2/" || true)"
    case "$code" in
      200|401) row PASS "container registry" "${REGISTRY%%/*} answers /v2/ ($code)" ;;
      *) if want images; then row FAIL "container registry" "${REGISTRY%%/*}: /v2/ answered '${code:-nothing}'"
         else row WARN "container registry" "${REGISTRY%%/*}: /v2/ answered '${code:-nothing}' (checked from here, not from the nodes)"; fi ;;
    esac
  fi

  print_table
  echo
  if [ "$FAILS" -gt 0 ]; then
    if [ "$DRY" -eq 1 ]; then
      echo "prereqs: $FAILS FAIL(s) -- continuing only because this is a --dry-run"
    else
      die "prereqs: $FAILS FAIL(s); fix them and re-run (nothing was changed)"
    fi
  else
    echo "prereqs: all checks passed"
  fi
}

# --- phase: images ---------------------------------------------------------------------------------------------

phase_images() {
  phase "images"
  resolve_tag
  if [ "$SKIP_BUILD" -eq 1 ]; then echo "  --skip-build: using $(image_ref orchestrator) and $(image_ref sims)"; return 0; fi
  detect_engine
  if [ -z "$ENGINE" ]; then
    if [ "$DRY" -eq 1 ]; then ENGINE=docker; else die "no container engine (docker or podman)"; fi
  fi
  local img
  for img in orchestrator sims; do
    log "  build $(image_ref "$img")"
    run "$ENGINE" build -f "$SCRIPT_DIR/images/$img.Dockerfile" --build-arg "OG_REVISION=$TAG" -t "$(image_ref "$img")" "$REPO"
    if [ "$NO_PUSH" -eq 0 ]; then
      [ -n "$REGISTRY" ] || die "--registry required to push (or --no-push)"
      log "  push $(image_ref "$img")"
      run "$ENGINE" push "$(image_ref "$img")"
    fi
  done
}

# --- phase: namespace ------------------------------------------------------------------------------------------

phase_namespace() {
  phase "namespace $NS"
  if k get namespace "$NS" >/dev/null 2>&1; then echo "  namespace $NS: present"
  else run k create namespace "$NS"; run k label namespace "$NS" app.kubernetes.io/part-of=opengrid --overwrite; fi
  if [ -n "$PULL_SECRET_FILE" ]; then
    if kn get secret og-pull >/dev/null 2>&1; then echo "  pull secret og-pull: present (kept)"
    else run kn create secret generic og-pull --type=kubernetes.io/dockerconfigjson \
      "--from-file=.dockerconfigjson=$PULL_SECRET_FILE"; fi
  fi
}

# --- phase: secrets --------------------------------------------------------------------------------------------

# shellcheck disable=SC2016  # a Go template, not a shell expansion
secret_keys() { kn get secret "$1" -o go-template='{{range $k, $v := .data}}{{$k}}{{"\n"}}{{end}}' 2>/dev/null || true; }
yaml_quote() { local v="$1"; v="${v//\\/\\\\}"; v="${v//\"/\\\"}"; printf '"%s"' "$v"; }
secret_header() {  # name -> a Secret manifest head; stringData follows
  printf 'apiVersion: v1\nkind: Secret\nmetadata:\n  name: %s\n  namespace: %s\n  labels:\n' "$1" "$NS"
  printf '    app.kubernetes.io/part-of: opengrid\n    app.kubernetes.io/instance: %s\ntype: Opaque\nstringData:\n' "$REL"
}

# ensure_secret NAME KEY=GENERATOR...: create the Secret or add its missing keys; present keys are never touched.
# GENERATOR is hex (openssl rand -hex 24), b64 (openssl rand -base64 24) or file:<path> (first line of a file).
ensure_secret() {
  local name="$1"; shift
  local have_keys spec key gen value added="" f="$WORK/$name.yaml" exists=0
  have_keys="$(secret_keys "$name")"
  kn get secret "$name" >/dev/null 2>&1 && exists=1
  if [ "$exists" -eq 1 ]; then printf 'stringData:\n' > "$f"; else secret_header "$name" > "$f"; fi
  for spec in "$@"; do
    key="${spec%%=*}"; gen="${spec#*=}"
    if printf '%s\n' "$have_keys" | grep -qx -- "$key"; then continue; fi
    case "$gen" in
      hex) value="$(openssl rand -hex 24)" ;;
      b64) value="$(openssl rand -base64 24)" ;;
      file:*) IFS= read -r value < "${gen#file:}" || true; [ -n "$value" ] || die "${gen#file:} is empty" ;;
      *) die "internal: generator $gen" ;;
    esac
    printf '  %s: %s\n' "$(yaml_quote "$key")" "$(yaml_quote "$value")" >> "$f"
    value=""
    added+=" $key"
  done
  if [ -z "$added" ]; then echo "  $name: present ($(printf '%s\n' "$have_keys" | grep -c .) keys, kept)"; return 0; fi
  if [ "$DRY" -eq 1 ]; then echo "  DRY: $name: would $([ "$exists" -eq 1 ] && echo add || echo create with):$added"
  elif [ "$exists" -eq 1 ]; then kn patch secret "$name" --type merge --patch-file "$f" >/dev/null; echo "  $name: added$added (not displayed)"
  else k create -f "$f" >/dev/null; echo "  $name: created with$added (not displayed)"; fi
  rm -f "$f"
}

# replace_env_secret NAME FILE: the Secret holds exactly the KEY=VALUE pairs of an owner-supplied env file.
replace_env_secret() {
  local name="$1" src="$2" f="$WORK/$1.yaml" body="$WORK/$1.body" line key value n=0
  : > "$body"
  while IFS= read -r line || [ -n "$line" ]; do
    [[ "$line" =~ ^[[:space:]]*([A-Za-z_][A-Za-z0-9_]*)=(.*)$ ]] || continue
    key="${BASH_REMATCH[1]}"; value="${BASH_REMATCH[2]}"
    value="${value%\"}"; value="${value#\"}"; value="${value%\'}"; value="${value#\'}"
    printf '  %s: %s\n' "$key" "$(yaml_quote "$value")" >> "$body"
    n=$((n + 1))
  done < "$src"
  value=""
  if [ "$n" -gt 0 ]; then { secret_header "$name"; cat "$body"; } > "$f"
  else secret_header "$name" | sed '$d' > "$f"; fi   # no keys: drop the empty stringData line
  rm -f "$body"
  if [ "$DRY" -eq 1 ]; then echo "  DRY: $name: would load $n key(s) from $src"
  elif kn get secret "$name" >/dev/null 2>&1; then k replace -f "$f" >/dev/null; echo "  $name: $n key(s) from $src (replaced, not displayed)"
  else k create -f "$f" >/dev/null; echo "  $name: $n key(s) from $src (created, not displayed)"; fi
  rm -f "$f"
}

phase_secrets() {
  phase "secrets (generated here, never printed or rotated)"
  if [ "$DRY" -eq 0 ] && ! k get namespace "$NS" >/dev/null 2>&1; then die "namespace $NS missing (run --phase namespace)"; fi
  local u specs=()

  if [ -n "$EXTERNAL_DB" ]; then ensure_secret og-db "OG_DB_PASSWORD=file:$DB_PASSWORD_FILE"
  else ensure_secret og-db OG_DB_PASSWORD=hex POSTGRES_PASSWORD=hex; fi
  ensure_secret og-mqtt OG_MQTT_ENGINE_PASSWORD=hex OG_MQTT_GUARDIAN_PASSWORD=hex OG_MQTT_API_PASSWORD=hex \
    OG_MQTT_SAFESTOP_PASSWORD=hex OG_MQTT_GRIDLINK_PASSWORD=hex
  ensure_secret og-mqtt-sim OG_MQTT_SIM_PASSWORD=hex OG_MQTT_SIMCTL_PASSWORD=hex OG_MQTT_CUSTOMER_PASSWORD=hex \
    OG_MQTT_UTILITY_PASSWORD=hex
  ensure_secret og-api-proxy OG_API_PROXY_SECRET=hex
  for u in $(ui_users); do specs+=("$u=b64"); done
  ensure_secret og-ui-users "${specs[@]}"

  if [ -n "$API_KEYS_FILE" ]; then replace_env_secret og-owner-keys "$API_KEYS_FILE"
  elif ! kn get secret og-owner-keys >/dev/null 2>&1; then
    : > "$WORK/empty.env"; replace_env_secret og-owner-keys "$WORK/empty.env"
  else echo "  og-owner-keys: present (kept; pass --api-keys-file to replace)"; fi
  if [ -n "$ANTHROPIC_FILE" ]; then replace_env_secret og-ai-agent "$ANTHROPIC_FILE"; fi

  # D-34 grid link (optional): the operator's [grid_link] override and its TLS files in one Secret.
  if [ -n "$GRID_LINK_CONFIG" ]; then
    if [ "$DRY" -eq 1 ]; then echo "  DRY: og-gridlink: would load grid_link.toml and $GRID_LINK_CERTS/*"
    else
      kn create secret generic og-gridlink "--from-file=grid_link.toml=$GRID_LINK_CONFIG" "--from-file=$GRID_LINK_CERTS" \
        --dry-run=client -o yaml > "$WORK/gridlink.yaml"
      if kn get secret og-gridlink >/dev/null 2>&1; then k replace -f "$WORK/gridlink.yaml" >/dev/null
      else k create -f "$WORK/gridlink.yaml" >/dev/null; fi
      rm -f "$WORK/gridlink.yaml"
      echo "  og-gridlink: grid_link.toml + $(find "$GRID_LINK_CERTS" -maxdepth 1 -type f | wc -l) TLS file(s) (not displayed)"
    fi
  fi

  # Ed25519 keys (guardian, safestop, trace anchor): bootstrap phase h inside the orchestrator image, streamed as a
  # tar to a private temp dir, loaded into the Secret, removed on exit.
  local want_files="guardian_ed25519.key guardian_ed25519.pub safestop_ed25519.key safestop_ed25519.pub trace_anchor_ed25519.key trace_anchor_ed25519.pub"
  local have_keys missing="" fk
  have_keys="$(secret_keys og-signing-keys)"
  for fk in $want_files; do printf '%s\n' "$have_keys" | grep -qx "$fk" || missing+=" $fk"; done
  if [ -z "$missing" ]; then echo "  og-signing-keys: present (6 files, kept)"
  elif [ -n "$have_keys" ]; then die "og-signing-keys exists but lacks:$missing (restore it from backup; keys are never regenerated over a partial set)"
  elif [ "$DRY" -eq 1 ]; then echo "  DRY: og-signing-keys: would run '$(image_ref orchestrator) keygen-tar' and create the Secret"
  else
    resolve_tag; detect_engine
    [ -n "$ENGINE" ] || die "a container engine is needed to generate the signing keys"
    mkdir -m 700 "$WORK/keys"
    "$ENGINE" run --rm --network none "$(image_ref orchestrator)" keygen-tar > "$WORK/keys.tar"
    tar -xf "$WORK/keys.tar" -C "$WORK/keys"
    rm -f "$WORK/keys.tar"
    for fk in $want_files; do [ -s "$WORK/keys/$fk" ] || die "keygen produced no $fk"; done
    kn create secret generic og-signing-keys --from-file="$WORK/keys" >/dev/null
    kn label secret og-signing-keys app.kubernetes.io/part-of=opengrid "app.kubernetes.io/instance=$REL" >/dev/null
    rm -rf "$WORK/keys"
    echo "  og-signing-keys: generated with the orchestrator's keygen CLIs (not displayed; back it up)"
  fi
}

# --- phase: deploy (and migrate/seed re-runs) --------------------------------------------------------------------

phase_deploy() {
  phase "deploy (helm upgrade --install $REL, chart $(sed -n 's/^version: *//p' "$CHART/Chart.yaml"))"
  resolve_tag
  build_helm_set
  if [ "$DRY" -eq 1 ]; then
    local out="${RENDER_DIR:-$WORK/render}"
    mkdir -p "$out"
    helm lint "$CHART" "${HELM_SET[@]}" | sed 's/^/  /'
    render_chart "$out/opengrid.yaml"
    echo "  DRY: rendered $(grep -c '^kind:' "$out/opengrid.yaml") objects to $out/opengrid.yaml:"
    grep '^kind:' "$out/opengrid.yaml" | sort | uniq -c | sed 's/^/   /'
    [ -n "$RENDER_DIR" ] || echo "  (temp dir, removed on exit; pass --render-dir to keep it)"
    echo "  DRY: helm upgrade --install $REL $CHART ${HELM_SET[*]} --wait --wait-for-jobs --timeout $TIMEOUT"
    return 0
  fi
  log "  installing; waits up to $TIMEOUT for Postgres, the migrations and seed Jobs and every Deployment"
  helm upgrade --install "$REL" "$CHART" "${HELM_SET[@]}" --wait --wait-for-jobs --timeout "$TIMEOUT"
}

rerun_job() {  # migrate|seed
  phase "re-run the $1 Job"
  run kn delete job -l "app.kubernetes.io/instance=$REL,app.kubernetes.io/component=$1" --ignore-not-found
  phase_deploy
}

# --- phase: verify -----------------------------------------------------------------------------------------------

phase_verify() {
  phase "verify"
  ROWS=(); FAILS=0
  if [ "$DRY" -eq 1 ]; then
    echo "  DRY: rollout status of every Deployment/StatefulSet, Job completion, bootstrap_check.py --live in og-api,"
    echo "       heartbeats of feeds/engine/guardian/safestop/settle/api, /og/api/health 200, gateway /og/ 401,"
    echo "       engine /metrics 200, https://$HOST/og/ 401$([ "$DEMO" -eq 1 ] && echo ', then the demo customers')"
    return 0
  fi
  local d api="deploy/$REL-api" out code p scheme=http
  for d in $(kn get deploy,statefulset -l "app.kubernetes.io/instance=$REL" -o name); do
    if kn rollout status "$d" --timeout="$TIMEOUT" >/dev/null 2>&1; then row PASS "rollout ${d#*/}" "ready"
    else row FAIL "rollout ${d#*/}" "not ready (kubectl -n $NS describe $d)"; fi
  done
  while read -r d out; do
    [ -n "$d" ] || continue
    check FAIL "job $d" "succeeded" "not succeeded (kubectl -n $NS logs job/$d)" ge "${out:-0}" 1
  done < <(kn get jobs -l "app.kubernetes.io/instance=$REL,app.kubernetes.io/component in (migrate,seed)" \
             -o jsonpath='{range .items[*]}{.metadata.name}{" "}{.status.succeeded}{"\n"}{end}')

  if kn exec "$api" -c api -- og-entrypoint probe-http http://127.0.0.1:8080/og/api/health >/dev/null 2>&1; then
    row PASS "og-api /og/api/health" "200 (loopback, inside the pod)"
  else row FAIL "og-api /og/api/health" "not 200"; fi
  if kn exec "$api" -c api -- og-entrypoint probe-http http://127.0.0.1:8081/og/ 401 >/dev/null 2>&1; then
    row PASS "gateway /og/ (no credentials)" "401 (basic auth enforced)"
  else row FAIL "gateway /og/ (no credentials)" "not 401"; fi
  if kn exec "deploy/$REL-engine" -- og-entrypoint probe-http http://127.0.0.1:9101/metrics >/dev/null 2>&1; then
    row PASS "og-engine /metrics" "200"
  else row FAIL "og-engine /metrics" "not 200"; fi
  log "  waiting 45 s for the first telemetry and invariant runs"; sleep 45
  for p in feeds engine guardian safestop settle api; do  # opengrid.health.model.ALL_PROCESSES
    if kn exec "$api" -c api -- og-entrypoint probe-heartbeat "$p" >/dev/null 2>&1; then row PASS "heartbeat $p" "fresh"
    else row FAIL "heartbeat $p" "stale or missing (og.heartbeat)"; fi
  done
  echo "  bootstrap_check.py --live (inside og-api):"
  if kn exec "$api" -c api -- env "OG_ZONES=$ZONES" og-entrypoint check --live | sed 's/^/    /'; then
    row PASS "seeded counts + live checks" "bootstrap_check.py PASS"
  else row FAIL "seeded counts + live checks" "bootstrap_check.py FAIL (see above)"; fi
  if have curl && [ -n "$HOST" ]; then
    [ -z "$TLS_SECRET" ] || scheme=https
    code="$(curl -sk -o /dev/null -w '%{http_code}' --max-time 10 "$scheme://$HOST/og/" || true)"
    check WARN "ingress $scheme://$HOST/og/" "401 (basic auth)" \
      "answered '${code:-nothing}' (DNS/LB not pointing here yet?)" test "$code" = 401
  fi
  if [ "$DEMO" -eq 1 ]; then
    log "  demo customers (dev/scripts/seed_demo_customers.py, inside og-api)"
    if kn exec "$api" -c api -- og-entrypoint demo-customers | sed 's/^/    /'; then row PASS "demo customers" "seeded"
    else row FAIL "demo customers" "seed_demo_customers.py failed"; fi
  fi
  kn get pods -l "app.kubernetes.io/instance=$REL" -o wide | sed 's/^/  /'
  print_table
  echo
  if [ "$FAILS" -gt 0 ]; then die "verify: $FAILS FAIL(s)"; fi
  echo "verify: PASS"
}

# --- phase: uninstall --------------------------------------------------------------------------------------------

phase_uninstall() {
  phase "uninstall $REL from $NS"
  if helm status "$REL" -n "$NS" >/dev/null 2>&1; then run helm uninstall "$REL" -n "$NS" --wait
  else echo "  release $REL: not installed"; fi
  if [ "$PURGE" -eq 1 ]; then
    [ "$YES" -eq 1 ] || die "--purge deletes namespace $NS with every Secret (signing keys!) and all data: add --yes"
    run k delete namespace "$NS" --wait=true
    echo "  namespace $NS deleted (Secrets, PVCs and data are gone)"
  else
    echo "  kept (re-install reuses them; --purge --yes deletes them):"
    kn get secrets,pvc -o name 2>/dev/null | sed 's/^/    /' || true
  fi
}

# --- main --------------------------------------------------------------------------------------------------------

echo "OpenGrid Kubernetes installer: release $REL, namespace $NS, phases [$PHASES]$([ "$DRY" -eq 1 ] && echo ' DRY RUN')"
for p in ${PHASES//,/ }; do
  case "$p" in
    prereqs|images|namespace|secrets|deploy|verify|migrate|seed|uninstall) ;;
    *) die "unknown phase '$p'" ;;
  esac
done
want uninstall && { phase_uninstall; exit 0; }
want prereqs && phase_prereqs
want images && phase_images
want namespace && phase_namespace
want secrets && phase_secrets
want deploy && phase_deploy
want migrate && rerun_job migrate
want seed && rerun_job seed
want verify && phase_verify
echo
echo "install: done$([ "$DRY" -eq 1 ] && echo ' (dry run: nothing was changed)')"
