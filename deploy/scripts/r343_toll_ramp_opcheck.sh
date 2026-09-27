#!/usr/bin/env bash
# r3.4.3 operational toll-ramp check (DISPATCH, for the release manager). Run as root on base AFTER the
# r3.4.3 deploy (DISPATCH 975b580 + SAFETY 70fc355):
#
#   deploy/scripts/r343_toll_ramp_opcheck.sh              # full run (about 3-4 minutes)
#   deploy/scripts/r343_toll_ramp_opcheck.sh verify <ISO>  # re-run only the verification from <ISO> (e.g. to
#                                                          # re-read K13 once the 15-min intervals have closed)
#
# What it does (each step printed):
#   1. pre-capture: K13/invariant counters, og.stop_event count, sub-LZ_AEN-00's current setpoint;
#      refuses (unless FORCE=1) when a manual target is already ACTIVE on the hub, a safe stop is engaged
#      on its scope, or its bank carried non-zero committed grants in the last 2 minutes;
#   2. a manual -3,000 kW (discharge) target on sub-LZ_AEN-00 through the operator API (the Fleet page's
#      route: POST /og/api/fleet/command, then /command/{id}/confirm), via Apache with Basic Auth;
#   3. mid-ramp, a 20 s hold: SIGSTOP og-engine (the whole unit cgroup), 20 s, SIGCONT;
#   4. mid-ramp, ONE item-level veto with lagging telemetry: a momentary SCADA utility LIMIT on
#      bank-sub-LZ_AEN-00 through ogsim.control (127.0.0.1:8091/api/inject, X-OGSim-Request: 1), lifted after
#      LIMIT_S seconds (DELETE /api/anomalies/{id});
#   5. waits for convergence (the last signed net setpoint within 1% of -3,000 kW), then cancels the target;
#   6. verifies from og.verdict, og.command_batch and the RT_ALLOCATION traces (see the criteria printed);
#   7. prints PASS/FAIL per criterion. A trap restores everything on any exit: SIGCONT og-engine, lift the
#      injected anomaly, cancel the manual target.
#
# Side effects to expect: during the 20 s hold og-engine does not beat (ALR-PROCESS-DOWN may open and then
# clear), and no hub gets a new batch for 20 s (leases are 30 s, so nothing lapses). K13's grant-gap
# threshold is 30 s, so the hold itself is not an outage gap.
#
# Credentials: the operator's Basic Auth password is read from /root/opengrid-ui-credentials.txt into a
# mode-600 curl config file (never on argv, never printed) that the trap deletes.

set -Eeuo pipefail

HUB="${HUB:-sub-LZ_AEN-00}"
BANK="${BANK:-bank-sub-LZ_AEN-00}"
TARGET_KW="${TARGET_KW:--3000}"          # +charge / -discharge: 3,000 kW discharge
TARGET_MINUTES="${TARGET_MINUTES:-15}"
HOLD_S="${HOLD_S:-20}"
LIMIT_KW="${LIMIT_KW:-500}"              # the momentary SCADA limit (below the mid-ramp setpoint)
LIMIT_S="${LIMIT_S:-4}"                  # ~2 cycles
CONVERGE_TIMEOUT_S="${CONVERGE_TIMEOUT_S:-300}"
OG_HOST="${OG_HOST:-base.tocy-net.net}"
OG_URL="${OG_URL:-https://${OG_HOST}}"
OGSIM_URL="${OGSIM_URL:-http://127.0.0.1:8091}"
CREDS_FILE="${CREDS_FILE:-/root/opengrid-ui-credentials.txt}"
OG_CONFIG="${OG_CONFIG:-/opt/opengrid/current/orchestrator/config/orchestrator.toml}"
PY="${PY:-/opt/opengrid/venv/bin/python}"
export PYTHONPATH="${PYTHONPATH:-/opt/opengrid/current/orchestrator/src}"
FORCE="${FORCE:-0}"
REASON_TAG="r343-opcheck"
INDUCED_CODES_RE='^G-(04|05|06|26|27)$'

WORK="$(mktemp -d /tmp/r343_opcheck.XXXXXX)"
chmod 700 "$WORK"
CURLCFG="$WORK/curl.cfg"
TARGET_TRACE_ID=""
ANOMALY_ID=""
ENGINE_STOPPED=0
RESULTS=()
FAILS=0

log() { printf '[%s] %s\n' "$(date -u +%H:%M:%S)" "$*"; }
psql_ro() {  # read-only, small SELECTs only; -At output
    runuser -u postgres -- psql -d og -X -q -At -v ON_ERROR_STOP=1 \
        -c "SET default_transaction_read_only = on; SET statement_timeout = '15s';" -c "$1"
}
result() {  # result <PASS|FAIL|INFO|WARN> <criterion> <detail>
    RESULTS+=("$(printf '%-4s  %-58s %s' "$1" "$2" "$3")")
    if [[ "$1" == FAIL ]]; then FAILS=$((FAILS + 1)); fi
}

# ---------------------------------------------------------------------------------------------- restore
restore() {
    local rc=$?
    set +e
    if [[ "$ENGINE_STOPPED" == 1 ]]; then
        log "restore: SIGCONT og-engine"
        systemctl kill -s SIGCONT og-engine
        ENGINE_STOPPED=0
    fi
    if [[ -n "$ANOMALY_ID" ]]; then
        log "restore: lifting ogsim anomaly $ANOMALY_ID"
        curl -sS -m 10 -X DELETE -H 'X-OGSim-Request: 1' "$OGSIM_URL/api/anomalies/$ANOMALY_ID" >/dev/null
        ANOMALY_ID=""
    fi
    if [[ -n "$TARGET_TRACE_ID" ]]; then
        log "restore: cancelling manual target $TARGET_TRACE_ID"
        og_api POST "/og/api/fleet/manual-targets/$TARGET_TRACE_ID/cancel" '{}' >/dev/null
        TARGET_TRACE_ID=""
    fi
    rm -rf "$WORK"
    exit "$rc"
}
trap restore EXIT
trap 'log "interrupted"; exit 130' INT TERM

# ---------------------------------------------------------------------------------------------- helpers
write_curl_config() {
    local pw
    pw="$(sed -n 's/^operator:[[:space:]]*//p' "$CREDS_FILE" | head -n 1 | tr -d '\r' | sed 's/[[:space:]]*$//')"
    if [[ -z "$pw" ]]; then
        log "FATAL: no operator line in $CREDS_FILE"
        exit 2
    fi
    umask 077
    pw="${pw//\\/\\\\}"
    pw="${pw//\"/\\\"}"
    printf 'user = "operator:%s"\n' "$pw" >"$CURLCFG"
    unset pw
}

og_api() {  # og_api <METHOD> <path> [json-body] -> response body on stdout; non-2xx is fatal
    local method="$1" path="$2" body="${3:-}" out code
    out="$WORK/resp.json"
    code="$(curl -sS -m 20 -K "$CURLCFG" --resolve "${OG_HOST}:443:127.0.0.1" \
        -X "$method" -H 'Content-Type: application/json' ${body:+--data "$body"} \
        -o "$out" -w '%{http_code}' "${OG_URL}${path}")"
    if [[ "$code" != 2* ]]; then
        log "API $method $path -> HTTP $code: $(head -c 300 "$out")"
        return 1
    fi
    cat "$out"
}

json_get() {  # json_get <key> < json
    "$PY" -c 'import json,sys; print(json.load(sys.stdin)[sys.argv[1]])' "$1"
}

last_signed_net_kw() {  # the latest PASS batch's net setpoint for $HUB since $1
    psql_ro "
      SELECT round((SELECT sum((i->>'p_kw_setpoint')::float) FROM jsonb_array_elements(t.payload->'items') i
                    WHERE i->>'hub_id' = '$HUB')::numeric, 1)
      FROM og.trace t JOIN og.verdict v ON v.command_batch_id = (t.payload->>'command_batch_id')::uuid
      WHERE t.stream_id = 'allocator-$BANK' AND t.event_class = 'RT_ALLOCATION' AND t.created_at >= '$1'
        AND v.outcome = 'PASS'
        AND EXISTS (SELECT 1 FROM jsonb_array_elements(t.payload->'items') i WHERE i->>'hub_id' = '$HUB')
      ORDER BY t.created_at DESC LIMIT 1"
}

# ---------------------------------------------------------------------------------------------- verify
verify() {
    local start="$1" induced_from="${2:-}" induced_to="${3:-}"
    log "verify: batches on $BANK / $HUB since $start"
    local has_pub
    has_pub="$(psql_ro "SELECT count(*) FROM information_schema.columns
                        WHERE table_schema='og' AND table_name='verdict' AND column_name='published_at'")"
    local pub_expr="NULL"
    [[ "$has_pub" == 1 ]] && pub_expr="v.published_at"
    psql_ro "
      COPY (
        SELECT t.created_at, t.payload->>'command_batch_id', t.payload->>'issued_at', t.payload->>'expires_at',
               (SELECT sum((i->>'p_kw_setpoint')::float) FROM jsonb_array_elements(t.payload->'items') i
                WHERE i->>'hub_id' = '$HUB'),
               coalesce(v.outcome, 'NO_VERDICT'), coalesce(v.vetoed_rule_ids::text, ''), v.signed_at, $pub_expr,
               (cb.command_batch_id IS NOT NULL)
        FROM og.trace t
        LEFT JOIN og.verdict v ON v.command_batch_id = (t.payload->>'command_batch_id')::uuid
        LEFT JOIN og.command_batch cb ON cb.command_batch_id = (t.payload->>'command_batch_id')::uuid
        WHERE t.stream_id = 'allocator-$BANK' AND t.event_class = 'RT_ALLOCATION' AND t.created_at >= '$start'
        ORDER BY t.created_at
      ) TO STDOUT WITH (FORMAT csv)" >"$WORK/batches.csv"
    local p_kw
    p_kw="$(psql_ro "SELECT p_kw FROM og.hub WHERE hub_id = '$HUB'")"

    "$PY" - "$WORK/batches.csv" "$p_kw" "$TARGET_KW" "$OG_CONFIG" "$has_pub" "$induced_from" "$induced_to" \
        >"$WORK/verify.out" <<'PYV'
import csv, sys
from datetime import datetime

path, p_kw, target_kw, cfg_path, has_pub, ind_from, ind_to = sys.argv[1:8]
p_kw, target_kw = float(p_kw), float(target_kw)
try:
    import tomllib
    with open(cfg_path, "rb") as fh:
        cycle_s = float(tomllib.load(fh).get("allocator", {}).get("cycle_interval_s", 2.0))
except Exception:
    cycle_s = 2.0
try:
    from opengrid.core.physics import FIRM_RAMP_MINUTES_TO_FULL_POWER as firm_min
except Exception:
    firm_min = 3.0
ramp = p_kw / firm_min / 60.0
bound = ramp * cycle_s

def ts(s):
    return datetime.fromisoformat(s.replace(" ", "T")) if s else None

ind_from, ind_to = ts(ind_from), ts(ind_to)
rows = list(csv.reader(open(path)))
signed_prev = None  # (net, expires_at)
max_step, max_at, reanchors, steps = 0.0, "", 0, 0
vetoes_by_code, induced_by_code = {}, {}
unpublished = no_batch_row = 0
last_net = None
for created, bid, issued, expires, net, outcome, codes, signed_at, published_at, has_cb in rows:
    if net == "":
        continue
    net, created_t, issued_t, expires_t = float(net), ts(created), ts(issued), ts(expires)
    if has_cb != "t":
        no_batch_row += 1
    if outcome != "PASS":
        in_window = ind_from is not None and ind_from <= created_t <= ind_to
        for code in [c.strip() for c in codes.strip("{}[]").replace('"', "").split(",") if c.strip()]:
            (induced_by_code if in_window else vetoes_by_code)[code] = (
                (induced_by_code if in_window else vetoes_by_code).get(code, 0) + 1
            )
        continue
    if has_pub == "1" and not published_at:
        unpublished += 1
        continue  # signed, never published: not where the hub is (r3.4.3 LOW)
    if signed_prev is not None and signed_prev[1] > issued_t:
        step = abs(net - signed_prev[0])
        steps += 1
        if step > max_step:
            max_step, max_at = step, created.replace(" ", "T")
    else:
        reanchors += 1  # first batch, or the previous signature's lease had lapsed
    signed_prev = (net, expires_t)
    last_net = net

print(f"bound_kw={bound:.1f} ramp_kw_per_s={ramp:.2f} cycle_s={cycle_s}")
print(f"max_step_kw={max_step:.1f} at={max_at} steps={steps} reanchors={reanchors}")
print(f"ratio={max_step / bound if bound else 0:.3f}")
print(f"last_net_kw={last_net}")
print("vetoes_outside=" + ",".join(f"{k}:{v}" for k, v in sorted(vetoes_by_code.items())))
print("vetoes_induced=" + ",".join(f"{k}:{v}" for k, v in sorted(induced_by_code.items())))
print(f"unpublished_pass={unpublished} no_command_batch_row={no_batch_row} batches={len(rows)}")
PYV
    cat "$WORK/verify.out" | sed 's/^/    /'
    local kv line
    local -A V=([vetoes_outside]="" [vetoes_induced]="" [last_net_kw]="")
    while IFS= read -r line; do
        for kv in $line; do V["${kv%%=*}"]="${kv#*=}"; done
    done <"$WORK/verify.out"

    # 1. every signed step within one cycle's G-04 bound of the physical (last signed, live-lease) setpoint
    if awk -v r="${V[ratio]}" 'BEGIN{exit !(r <= 1.0)}'; then
        result PASS "signed steps <= one cycle's G-04 bound" "max ${V[max_step_kw]} kW vs bound ${V[bound_kw]} kW (ratio ${V[ratio]})"
    else
        result FAIL "signed steps <= one cycle's G-04 bound" "max ${V[max_step_kw]} kW at ${V[at]} vs bound ${V[bound_kw]} kW (ratio ${V[ratio]})"
    fi
    # 2. convergence
    if [[ -n "${V[last_net_kw]}" && "${V[last_net_kw]}" != None ]] &&
        awk -v n="${V[last_net_kw]}" -v t="$TARGET_KW" 'BEGIN{d=n-t; if(d<0)d=-d; exit !(d <= 0.01*(t<0?-t:t))}'; then
        result PASS "converged to ${TARGET_KW} kW" "last signed net ${V[last_net_kw]} kW"
    else
        result FAIL "converged to ${TARGET_KW} kW" "last signed net ${V[last_net_kw]:-none} kW"
    fi
    # 3. vetoes by G-code outside the induced window
    local bad=""
    IFS=',' read -r -a arr <<<"${V[vetoes_outside]}"
    for kv in "${arr[@]}"; do
        [[ -z "$kv" ]] && continue
        [[ "${kv%%:*}" =~ $INDUCED_CODES_RE ]] && bad+="$kv "
    done
    if [[ -z "$bad" ]]; then
        result PASS "0 G-04/05/06/26/27 vetoes outside the induced one" "outside: ${V[vetoes_outside]:-none}"
    else
        result FAIL "0 G-04/05/06/26/27 vetoes outside the induced one" "outside: $bad"
    fi
    if [[ -n "$induced_from" ]]; then
        if [[ -n "${V[vetoes_induced]}" ]]; then
            result INFO "induced veto window (SCADA limit)" "${V[vetoes_induced]}"
        else
            result WARN "induced veto window (SCADA limit)" "no veto observed in the window (limit not binding?)"
        fi
    fi
    result INFO "batches / unpublished PASS / no og.command_batch row" "${V[batches]} / ${V[unpublished_pass]} / ${V[no_command_batch_row]}"

    # 4. no K13 violation since the start
    local k13
    k13="$(psql_ro "SELECT count(*) FROM og.invariant_violation WHERE check_name LIKE 'K13%' AND detected_at >= '$start'")"
    if [[ "$k13" == 0 ]]; then
        result PASS "no K13 violation since start" "0 rows (intervals are checked once closed: re-run 'verify $start' after the next quarter hour)"
    else
        result FAIL "no K13 violation since start" "$k13 rows: $(psql_ro "SELECT string_agg(check_name || ' ' || coalesce(scope->>'obligation_id','-'), '; ') FROM og.invariant_violation WHERE check_name LIKE 'K13%' AND detected_at >= '$start'")"
    fi
}

report() {
    echo
    echo "================ r3.4.3 toll-ramp opcheck: $HUB ================"
    printf '%s\n' "${RESULTS[@]}"
    echo "================================================================"
    if [[ "$FAILS" == 0 ]]; then echo "OVERALL: PASS"; else echo "OVERALL: FAIL ($FAILS)"; fi
}

# ---------------------------------------------------------------------------------------------- verify-only mode
if [[ "${1:-}" == verify ]]; then
    [[ -n "${2:-}" ]] || { echo "usage: $0 verify <ISO start> [induced_from induced_to]"; exit 2; }
    verify "$2" "${3:-}" "${4:-}"
    report
    [[ "$FAILS" == 0 ]]
    exit $?
fi

[[ "$(id -u)" == 0 ]] || { echo "run as root"; exit 2; }

# ---------------------------------------------------------------------------------------------- 1. pre-capture
START="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
log "1. pre-capture (start $START)"
psql_ro "SELECT check_name || '=' || coalesce(last_violations::text,'-') FROM og.invariant_check ORDER BY 1" |
    tr '\n' ' ' | sed 's/^/    counters: /'
echo
STOPS_BEFORE="$(psql_ro "SELECT count(*) FROM og.stop_event")"
log "    og.stop_event rows: $STOPS_BEFORE"
log "    $HUB telemetry: $(psql_ro "SELECT 'p_kw=' || coalesce(p_kw::text,'-') || ' lease_expires_at=' || coalesce(lease_expires_at::text,'-') || ' last_seen_at=' || coalesce(last_seen_at::text,'-') FROM og.hub_state WHERE hub_id = '$HUB'")"
log "    $HUB last signed net (10 min): $(last_signed_net_kw "$(date -u -d '-10 min' +%Y-%m-%dT%H:%M:%S+00:00)")"
ENGAGED="$(psql_ro "
    SELECT count(*) FROM (
      SELECT DISTINCT ON (scope_kind, scope_ref) scope_kind, scope_ref, action FROM og.stop_event
      ORDER BY scope_kind, scope_ref, created_at DESC) s
    WHERE s.action = 'ENGAGE' AND (s.scope_kind = 'FLEET' OR (s.scope_kind = 'BANK' AND s.scope_ref = '$BANK')
          OR (s.scope_kind = 'ZONE' AND s.scope_ref = (SELECT zone FROM og.hub WHERE hub_id = '$HUB')))")"
BUSY="$(psql_ro "SELECT count(*) FROM og.grant g
                 WHERE g.bank_id = '$BANK' AND g.granted_kw > 0 AND g.created_at > now() - interval '2 minutes'")"
write_curl_config
ACTIVE="$(og_api GET "/og/api/fleet/manual-targets" | "$PY" -c "
import json,sys; print(sum(1 for i in json.load(sys.stdin)['items'] if i['hub_id']=='$HUB' and i['status']=='ACTIVE'))")"
log "    engaged stops on scope: $ENGAGED; active manual targets on hub: $ACTIVE; recent non-zero grants on bank: $BUSY"
if [[ "$FORCE" != 1 && ( "$ENGAGED" != 0 || "$ACTIVE" != 0 || "$BUSY" != 0 ) ]]; then
    log "REFUSING: the hub is not free (set FORCE=1 to override)"
    exit 3
fi

# ---------------------------------------------------------------------------------------------- 2. manual target
log "2. manual target ${TARGET_KW} kW on $HUB for ${TARGET_MINUTES} min"
PROPOSAL="$(og_api POST "/og/api/fleet/command" \
    "{\"hub_id\":\"$HUB\",\"p_kw_setpoint\":$TARGET_KW,\"reason\":\"$REASON_TAG\",\"duration_minutes\":$TARGET_MINUTES}" |
    json_get proposal_id)"
CONFIRM="$(og_api POST "/og/api/fleet/command/$PROPOSAL/confirm" '{}')"
TARGET_TRACE_ID="$(json_get trace_id <<<"$CONFIRM")"
log "    RAMPING, trace_id=$TARGET_TRACE_ID"

# ---------------------------------------------------------------------------------------------- 3. 20 s hold
sleep 8
log "3. mid-ramp hold: net now $(last_signed_net_kw "$START") kW; SIGSTOP og-engine for ${HOLD_S}s"
ENGINE_STOPPED=1
systemctl kill -s SIGSTOP og-engine
sleep "$HOLD_S"
systemctl kill -s SIGCONT og-engine
ENGINE_STOPPED=0
log "    SIGCONT og-engine"

# ---------------------------------------------------------------------------------------------- 4. one veto
sleep 6
log "4. mid-ramp SCADA LIMIT ${LIMIT_KW} kW on $BANK for ${LIMIT_S}s (net now $(last_signed_net_kw "$START") kW)"
INDUCED_FROM="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
INJECT_BODY="{\"type\":\"utility_instruction\",\"target\":\"$BANK\",\"params\":{\"mode\":\"limit\",\"limit_kw\":$LIMIT_KW},\"duration\":$((LIMIT_S + 30))}"
log "    POST $OGSIM_URL/api/inject $INJECT_BODY"
ANOMALY_ID="$(curl -sS -m 10 -X POST -H 'X-OGSim-Request: 1' -H 'Content-Type: application/json' \
    --data "$INJECT_BODY" "$OGSIM_URL/api/inject" | "$PY" -c 'import json,sys; print(json.load(sys.stdin)["anomaly"]["id"])')"
sleep "$LIMIT_S"
curl -sS -m 10 -X DELETE -H 'X-OGSim-Request: 1' "$OGSIM_URL/api/anomalies/$ANOMALY_ID" >/dev/null
ANOMALY_ID=""
sleep 4  # two cycles for the lift to reach the guardian
INDUCED_TO="$(date -u +%Y-%m-%dT%H:%M:%S+00:00)"
log "    lifted"

# ---------------------------------------------------------------------------------------------- 5. converge
log "5. waiting for convergence to ${TARGET_KW} kW (timeout ${CONVERGE_TIMEOUT_S}s)"
deadline=$((SECONDS + CONVERGE_TIMEOUT_S))
while ((SECONDS < deadline)); do
    net="$(last_signed_net_kw "$START")"
    if [[ -n "$net" ]] && awk -v n="$net" -v t="$TARGET_KW" 'BEGIN{d=n-t; if(d<0)d=-d; exit !(d <= 0.01*(t<0?-t:t))}'; then
        log "    converged: $net kW"
        break
    fi
    sleep 4
done
sleep 10  # hold at target a few cycles, then cancel
log "    cancelling the manual target"
og_api POST "/og/api/fleet/manual-targets/$TARGET_TRACE_ID/cancel" '{}' >/dev/null
TARGET_TRACE_ID=""

# ---------------------------------------------------------------------------------------------- 6-7. verify
verify "$START" "$INDUCED_FROM" "$INDUCED_TO"
STOPS_AFTER="$(psql_ro "SELECT count(*) FROM og.stop_event")"
if [[ "$STOPS_AFTER" == "$STOPS_BEFORE" ]]; then
    result PASS "og.stop_event unchanged" "$STOPS_BEFORE rows"
else
    result FAIL "og.stop_event unchanged" "$STOPS_BEFORE -> $STOPS_AFTER"
fi
log "    counters after: $(psql_ro "SELECT string_agg(check_name || '=' || coalesce(last_violations::text,'-'), ' ' ORDER BY check_name) FROM og.invariant_check")"
report
echo "re-verify K13 later with: $0 verify $START $INDUCED_FROM $INDUCED_TO"
[[ "$FAILS" == 0 ]]
