#!/bin/bash
# r3.4.3 operational check of the utility customer API (D-33) as og-util-aen, THROUGH Apache.
# Run as root on base after the deploy. Credentials come from /root/opengrid-utility-credentials.txt
# (user= / password= / url=) and are never printed or put in argv (curl reads them from stdin, -K -).
#
# By design a utility can only call inside its toll reservation window (16:30-18:00 CT): outside it there is
# no deployable obligation. So there are two modes:
#   default (any time outside the window, e.g. 04:50): the REFUSED path. One small call (-100 kW, 1 min) is
#     sent; it must be refused 404 R-CALL-NOT-FOUND and recorded (og.dispatch_call REFUSED + its trace
#     DISPATCH_CALL_REFUSED + ALR-UTILITY-CALL-REFUSED); its status must read REFUSED; a cancel of it must
#     answer 409 R-CALL-ALREADY-ENDED. Nothing is deployed: today's 16:30 toll is untouched.
#   ACCEPT=1 (only inside the window AND before the sim's planned call; `journalctl -u og-sim-utility | grep
#     utility_day` shows planned_start): the ACCEPTED path. -100 kW for 1 min, then cancelled at once:
#     201 ACCEPTED, then cancel 200 COMPLETED; ledger ACCEPTED + trace DISPATCH_CALL + DISPATCH_CALL_END.
#     It overlaps nothing if run before the planned start; the obligation returns to its 0 kW hold next cycle.
# Read-only SQL against the production DB (og on 5432) as postgres, for the verification only.
set -uo pipefail

CRED=/root/opengrid-utility-credentials.txt
DB=${OG_DB_NAME:-og}
ACCEPT=${ACCEPT:-0}
FAIL=0
pass() { echo "PASS $*"; }
fail() { echo "FAIL $*"; FAIL=1; }

[ "$(id -u)" = 0 ] || { echo "run as root" >&2; exit 2; }
[ -r "$CRED" ] || { echo "missing $CRED" >&2; exit 2; }
U=$(sed -n 's/^user=//p' "$CRED"); P=$(sed -n 's/^password=//p' "$CRED"); URL=$(sed -n 's/^url=//p' "$CRED")
[ -n "$U" ] && [ -n "$P" ] && [ -n "$URL" ] || { echo "user=/password=/url= missing in $CRED" >&2; exit 2; }
case "$U" in *[!A-Za-z0-9_-]*) echo "unexpected user name in $CRED" >&2; exit 2;; esac
BASE=${URL%/}
HOST=$(printf '%s' "$BASE" | sed -E 's#^https?://([^/:]+).*#\1#')
WORK=$(mktemp -d /dev/shm/og_opcheck.XXXXXX); chmod 700 "$WORK"; trap 'rm -rf "$WORK"' EXIT

# api METHOD PATH [JSON] -> prints the HTTP status; the body goes to $WORK/body
api() {
    local method=$1 path=$2 data=${3:-}
    local args=(-sS -o "$WORK/body" -w '%{http_code}' --max-time 20 -X "$method"
                --resolve "$HOST:443:127.0.0.1" -H 'Accept: application/json')
    [ -n "$data" ] && args+=(-H 'Content-Type: application/json' --data "$data")
    printf 'user = "%s:%s"\n' "$U" "$P" | curl -K - "${args[@]}" "$BASE$path"
}
field() { python3 -c 'import json,sys; b=json.load(open(sys.argv[1])); d=b.get("detail") if isinstance(b.get("detail"),dict) else b
v=d.get(sys.argv[2]) if isinstance(d,dict) else None; print("" if v is None else v)' "$WORK/body" "$1"; }
sql() { runuser -u postgres -- psql -X -At -d "$DB" -v ON_ERROR_STOP=1 -c "$1"; }

T0=$(date -u +%Y-%m-%dT%H:%M:%SZ)
KEY="opcheck-$(date -u +%Y%m%dT%H%M%S)-$$"
code=$(api GET /me); [ "$code" = 200 ] && [ "$(field utility_id)" = AUSTIN_ENERGY ] && pass "me: AUSTIN_ENERGY" || fail "me: HTTP $code"
code=$(api GET /obligations); [ "$code" = 200 ] && pass "obligations: HTTP 200 (today: $(field today | cut -c1-60))" || fail "obligations: HTTP $code"

BODY=$(printf '{"kw": -100, "duration_minutes": 1, "idempotency_key": "%s", "reason": "r3.4.3 opcheck"}' "$KEY")
code=$(api POST /calls "$BODY"); CALL=$(field call_id); REASON=$(field reason_code)
echo "INFO call: HTTP $code call_id=$CALL reason=$REASON"
if [ "$ACCEPT" = 1 ]; then
    [ "$code" = 201 ] && [ -n "$CALL" ] && pass "call ACCEPTED" || fail "expected 201 ACCEPTED, got $code $REASON"
else
    [ "$code" = 404 ] && [ "$REASON" = R-CALL-NOT-FOUND ] && [ -n "$CALL" ] \
        && pass "call refused outside the window (404 R-CALL-NOT-FOUND, recorded)" || fail "expected 404 R-CALL-NOT-FOUND, got $code $REASON"
fi
[ -n "$CALL" ] || { echo "RESULT FAIL (no call_id)"; exit 1; }
case "$CALL" in *[!0-9a-f-]*) echo "RESULT FAIL (bad call_id)"; exit 1;; esac

code=$(api GET "/calls/$CALL"); STATE=$(field state)
MEASURED=$(python3 -c 'import json,sys; print("delivery_measured" in json.load(open(sys.argv[1])))' "$WORK/body")
DSTATE=$(field delivery_state)
echo "INFO status: HTTP $code state=$STATE delivery_measured-present=$MEASURED delivery_state=$DSTATE"
if [ "$ACCEPT" = 1 ]; then
    [ "$code" = 200 ] && case "$STATE" in ACCEPTED|ACTIVE|RAMPING|DELIVERING) true;; *) false;; esac \
        && [ "$MEASURED" = True ] && pass "status $STATE with delivery fields" || fail "status: HTTP $code $STATE"
else
    [ "$code" = 200 ] && [ "$STATE" = REFUSED ] && [ "$MEASURED" = True ] && pass "status REFUSED with delivery fields" \
        || fail "status: HTTP $code $STATE measured=$MEASURED"
fi

code=$(api POST "/calls/$CALL/cancel" '{}'); CREASON=$(field reason_code); CSTATE=$(field state)
if [ "$ACCEPT" = 1 ]; then
    [ "$code" = 200 ] && [ "$CSTATE" = COMPLETED ] && pass "cancel 200 COMPLETED" || fail "cancel: HTTP $code $CSTATE $CREASON"
else
    [ "$code" = 409 ] && [ "$CREASON" = R-CALL-ALREADY-ENDED ] && pass "cancel of a refused call: 409 R-CALL-ALREADY-ENDED" \
        || fail "cancel: HTTP $code $CREASON"
fi

ROW=$(sql "SELECT outcome||'|'||origin||'|'||principal||'|'||coalesce(reason_code,'-')||'|'||(trace_id IS NOT NULL)
           FROM og.dispatch_call WHERE call_id = '$CALL'")
echo "INFO ledger: $ROW"
WANT=$([ "$ACCEPT" = 1 ] && echo "ACCEPTED|UTILITY|$U|-|true" || echo "REFUSED|UTILITY|$U|R-CALL-NOT-FOUND|true")
[ "$ROW" = "$WANT" ] && pass "og.dispatch_call row" || fail "og.dispatch_call row: want $WANT"
EV=$(sql "SELECT string_agg(event_class, ',' ORDER BY seq) FROM og.trace
          WHERE stream_id = 'dispatch_call:$U' AND created_at >= '$T0'")
echo "INFO trace events since $T0: $EV"
if [ "$ACCEPT" = 1 ]; then
    case "$EV" in *DISPATCH_CALL*DISPATCH_CALL_END*) pass "trace call + end";; *) fail "trace: $EV";; esac
else
    case "$EV" in *DISPATCH_CALL_REFUSED*) pass "trace refusal";; *) fail "trace: $EV";; esac
    AL=$(sql "SELECT count(*) FROM og.alert WHERE rule = 'ALR-UTILITY-CALL-REFUSED' AND scope_ref = '$U'")
    [ "${AL:-0}" -ge 1 ] && pass "operator alert ALR-UTILITY-CALL-REFUSED" || fail "no ALR-UTILITY-CALL-REFUSED"
fi
[ "$(sql "SELECT count(*) FROM og.as_deployment WHERE call_id = '$CALL' AND cancelled_at IS NULL AND end_at > now()")" = 0 ] \
    && pass "nothing left deployed" || fail "a deployment is still active for $CALL"

[ "$FAIL" = 0 ] && echo "RESULT PASS" || echo "RESULT FAIL"
exit "$FAIL"
