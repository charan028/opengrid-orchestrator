# shellcheck shell=bash
# deploy/scripts/lib_secrets.sh -- env-file and secret helpers shared by bootstrap_from_scratch.sh and
# create_schema.sh (sourced, not executed). Values are read into variables and written with printf, a bash
# builtin, so a secret never appears in a process's argv or in any output. Callers set DRY=0|1.

log() { printf '%s %s\n' "$(date +%H:%M:%S)" "$*"; }
die() { echo "ERROR: $*" >&2; exit 1; }
run() { if [ "${DRY:-0}" -eq 1 ]; then echo "  DRY: $*"; else "$@"; fi; }

env_get() { [ -f "$1" ] && sed -n "s/^$2=//p" "$1" | tail -1 | sed -e "s/^'//" -e "s/'\$//" || true; }
env_set() {  # file key value
  local f="$1" k="$2" v="$3" tmp
  tmp="$(mktemp "${f}.XXXXXX")"
  [ -f "$f" ] && grep -v "^$k=" "$f" > "$tmp" || true
  printf '%s=%s\n' "$k" "$v" >> "$tmp"
  mv "$tmp" "$f"
}
secure_env() { [ -f "$1" ] || return 0; chown "${2:-root:opengrid}" "$1"; chmod "${3:-640}" "$1"; }
gen_secret() { openssl rand -hex 24; }
# env_ensure file key [owner] [mode]: generate the key when absent. Returns 0 if present, 10 if generated now.
env_ensure() {
  local f="$1" k="$2"
  if [ -n "$(env_get "$f" "$k")" ]; then echo "  $k: present"; return 0; fi
  if [ "${DRY:-0}" -eq 1 ]; then echo "  DRY: generate $k into $f"; return 10; fi
  mkdir -p "$(dirname "$f")"
  env_set "$f" "$k" "$(gen_secret)"
  secure_env "$f" "${3:-root:opengrid}" "${4:-640}"
  echo "  $k: generated (not displayed)"
  return 10
}
