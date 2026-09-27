#!/bin/bash
# tests-perf/watchdog.sh -- PRODUCTION GUARDRAIL for a perf run (root, base server). Read-only on production.
#
# Every 15 s (the iostat interval is the clock) it samples:
#   - production og-engine cycle p99: the og_engine_cycle_latency_ms{quantile="p99"} gauge on 127.0.0.1:9101
#     (the rolling A11 window the engine also traces as CYCLE_LATENCY);
#   - production pgdata device utilisation (dm-6 = fobos--vg-pgdata) over the 15 s window, `iostat -dxy`;
#   - production fresh hubs: hub_health_counts.online from the loopback-exempt GET 127.0.0.1:8080/og/api/health.
#   - host MemAvailable and the size of og_perf's tmpfs tablespace (/dev/shm/ogperf_ts);
# and ABORTS the perf run when p99 > MAX_P99_MS (400), util > MAX_UTIL (50 %), fresh < MIN_FRESH (3509),
# MemAvailable < MIN_MEM_MB (3000) or the tablespace > MAX_TMPFS_MB (3500):
# it writes <run>/ABORT and one JSON line to <run>/aborts.jsonl, then stops the perf stack (stack.sh down:
# exact units/PIDs) and the sampler. An unreachable production health/metrics endpoint counts as a breach.
#
#   bash tests-perf/watchdog.sh [--run DIR]      (normally started by campaign.sh as unit ogperf-watchdog)
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RUN="$SCRIPT_DIR/.run"
[ "${1:-}" = "--run" ] && RUN="$2"
MAX_P99_MS="${MAX_P99_MS:-400}"
MAX_UTIL="${MAX_UTIL:-50}"
MIN_FRESH="${MIN_FRESH:-3509}"
DEV="${PGDATA_DEV:-dm-6}"
MIN_MEM_MB="${MIN_MEM_MB:-3000}"      # host MemAvailable floor (og_perf's data files live on tmpfs)
MAX_TMPFS_MB="${MAX_TMPFS_MB:-3500}"  # og_perf tablespace ceiling on /dev/shm
TS_DIR=/dev/shm/ogperf_ts
PROD_METRICS=http://127.0.0.1:9101/metrics
PROD_HEALTH=http://127.0.0.1:8080/og/api/health
OUT="$RUN/data/guard.jsonl"
mkdir -p "$RUN/data"

while true; do
  # iostat -y skips the since-boot report; the single 15 s report is the sample window.
  line="$(iostat -dxy "$DEV" 15 1 | awk -v d="$DEV" '$1==d' | tail -1)"
  util="$(echo "$line" | awk '{print $NF}')"
  await="$(echo "$line" | awk '{print $12}')"
  p99="$(curl -s -m 3 "$PROD_METRICS" | awk -F' ' '/^og_engine_cycle_latency_ms\{quantile="p99"\}/ {print $2}')"
  pmax="$(curl -s -m 3 "$PROD_METRICS" | awk -F' ' '/^og_engine_cycle_latency_ms\{quantile="max"\}/ {print $2}')"
  fresh="$(curl -s -m 3 "$PROD_HEALTH" | python3 -c 'import json,sys
try:
    print(json.load(sys.stdin).get("hub_health_counts", {}).get("online", ""))
except Exception:
    print("")')"
  ts="$(date -Is)"
  reasons=""
  [ -z "$p99" ] && reasons+="prod_metrics_unreachable;"
  [ -z "$fresh" ] && reasons+="prod_health_unreachable;"
  [ -z "$util" ] && util=0
  mem="$(awk '/^MemAvailable:/ {printf "%d", $2/1024}' /proc/meminfo)"
  tsmb="$(du -sm "$TS_DIR" 2>/dev/null | awk '{print $1}')"; tsmb="${tsmb:-0}"
  [ "$mem" -lt "$MIN_MEM_MB" ] && reasons+="mem_available_${mem}MB<${MIN_MEM_MB};"
  [ "$tsmb" -gt "$MAX_TMPFS_MB" ] && reasons+="tmpfs_tablespace_${tsmb}MB>${MAX_TMPFS_MB};"
  if [ -n "$p99" ] && awk -v a="$p99" -v b="$MAX_P99_MS" 'BEGIN{exit !(a>b)}'; then reasons+="prod_p99_${p99}ms>${MAX_P99_MS};"; fi
  if awk -v a="$util" -v b="$MAX_UTIL" 'BEGIN{exit !(a>b)}'; then reasons+="pgdata_util_${util}%>${MAX_UTIL};"; fi
  if [ -n "$fresh" ] && [ "$fresh" -lt "$MIN_FRESH" ]; then reasons+="prod_fresh_${fresh}<${MIN_FRESH};"; fi
  printf '{"ts":"%s","prod_p99_ms":%s,"prod_max_ms":%s,"pgdata_util":%s,"pgdata_w_await_ms":%s,"prod_fresh":%s,"mem_available_mb":%s,"tmpfs_mb":%s,"breach":"%s"}\n' \
    "$ts" "${p99:-null}" "${pmax:-null}" "$util" "${await:-null}" "${fresh:-null}" "$mem" "$tsmb" "$reasons" >> "$OUT"
  # One abort per breach episode: while <run>/ABORT exists the stack is already down (campaign.sh clears it
  # when it resumes), so later breaching samples are only logged above.
  if [ -n "$reasons" ] && [ ! -f "$RUN/ABORT" ]; then
    stage="$(cat "$RUN/STAGE" 2>/dev/null || echo unknown)"
    printf '{"ts":"%s","stage":"%s","reasons":"%s","prod_p99_ms":%s,"pgdata_util":%s,"prod_fresh":%s}\n' \
      "$ts" "$stage" "$reasons" "${p99:-null}" "$util" "${fresh:-null}" >> "$RUN/aborts.jsonl"
    echo "$ts $stage $reasons" > "$RUN/ABORT"
    echo "$ts ABORT ($stage): $reasons -- stopping the perf stack"
    systemctl stop ogperf-sampler.service >/dev/null 2>&1 || true
    bash "$SCRIPT_DIR/stack.sh" --run "$RUN" down
    # keep watching (the campaign decides whether to resume once production is healthy again)
  fi
done
