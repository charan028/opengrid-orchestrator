"""End-to-end smoke test for the deployed OpenGrid Orchestrator (BUILD.md merge task C).

Run on the server (it needs the live Postgres `og` database, the live MQTT broker, and
`/root/opengrid-ui-credentials.txt`), as root or as a user that can read that credentials file:

    python3 tests-e2e/smoke.py

Exits 0 only if every check passes; otherwise prints a PASS/FAIL line per acceptance item (A1-A11,
`01-saturday-delivery-plan.md`) and exits 1. Never prints the UI credentials it reads, only that it
read them.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field

LOCAL_HEALTH = "http://127.0.0.1:8080/og/api/health"  # loopback-exempt (require_loopback_health_probe)
API_BASE = "https://base.tocy-net.net/og/api"  # through Apache, so X-Remote-User gets set (api/auth.py)
UI_URL = "https://base.tocy-net.net/og/"
CONTROL_BASE = "http://127.0.0.1:8091"
CREDS_PATH = "/root/opengrid-ui-credentials.txt"


@dataclass
class Results:
    lines: list[str] = field(default_factory=list)
    ok: bool = True

    def record(self, item: str, passed: bool, detail: str) -> None:
        status = "PASS" if passed else "FAIL"
        self.lines.append(f"[{status}] {item}: {detail}")
        if not passed:
            self.ok = False


def _psql(sql: str) -> str:
    """Run one SQL statement as the opengrid user against the live `og` database, return stdout."""
    cmd = [
        "runuser",
        "-u",
        "opengrid",
        "--",
        "bash",
        "-c",
        f"set -a; . /etc/opengrid/secrets.env; set +a; "
        f"PGPASSWORD=$OG_DB_PASSWORD psql -h 127.0.0.1 -U opengrid -d og -tA -c \"{sql}\"",
    ]
    out = subprocess.run(cmd, capture_output=True, text=True, timeout=15, check=False)
    return out.stdout.strip()


def _get(url: str, *, auth: tuple[str, str] | None = None, timeout: float = 10.0) -> tuple[int, bytes]:
    req = urllib.request.Request(url)
    if auth is not None:
        import base64

        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310 -- local/trusted URLs only
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _post(
    url: str, payload: dict, *, auth: tuple[str, str] | None = None, timeout: float = 10.0
) -> tuple[int, bytes]:
    data = json.dumps(payload).encode()
    req = urllib.request.Request(url, data=data, headers={"Content-Type": "application/json"}, method="POST")
    if auth is not None:
        import base64

        token = base64.b64encode(f"{auth[0]}:{auth[1]}".encode()).decode()
        req.add_header("Authorization", f"Basic {token}")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:  # noqa: S310
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


def _read_ui_credentials() -> tuple[str, str] | None:
    """Read operator/viewer creds from the root-only file; never returns/prints the values in a log."""
    try:
        text = open(CREDS_PATH, encoding="utf-8").read()  # noqa: SIM115
    except OSError:
        return None
    match = re.search(r"operator\s*[:=]\s*(\S+)", text)
    if not match:
        return None
    return "operator", match.group(1)


def check_live_feeds(results: Results) -> None:
    count = _psql(
        "SELECT count(*) FROM og.feed_obs WHERE source='ERCOT' AND ts > now() - interval '2 hours'"
    )
    n = int(count) if count.isdigit() else 0
    results.record("A1 live feeds (feed_obs)", n > 0, f"{n} recent ERCOT feed_obs rows")


def check_hubs_online(results: Results, auth: tuple[str, str] | None) -> None:
    status_code, body = _get(f"{API_BASE}/fleet/hubs", auth=auth)
    if status_code != 200:
        results.record("A2 fleet twin 2000 hubs", False, f"GET /fleet/hubs -> {status_code}")
        return
    try:
        items = json.loads(body).get("items", [])
    except json.JSONDecodeError:
        items = []
    online = sum(1 for h in items if h.get("health") == "online")
    results.record(
        "A2 fleet twin 2000 hubs online",
        len(items) >= 2000 and online >= 1900,
        f"{len(items)} hubs total, {online} online",
    )


def check_concurrent_commitments(results: Results) -> None:
    row = _psql(
        "SELECT count(*), count(DISTINCT c.customer_id) FROM og.commitment cm "
        "JOIN og.obligation o ON o.obligation_id = cm.obligation_id "
        "JOIN og.contract c ON c.contract_id = o.contract_id"
    )
    parts = row.split("|") if row else ["0", "0"]
    n_commitments = int(parts[0]) if parts[0].strip().isdigit() else 0
    n_customers = int(parts[1]) if len(parts) > 1 and parts[1].strip().isdigit() else 0
    results.record(
        "A4/A5 selector committing obligations for several customers",
        n_commitments > 0 and n_customers > 1,
        f"{n_commitments} commitments across {n_customers} distinct customers",
    )


def check_allocator_guardian_fleet(results: Results) -> None:
    grants = _psql("SELECT count(*) FROM og.grant")
    n_grants = int(grants) if grants.isdigit() else 0
    results.record("A4 allocator grants persisted", n_grants > 0, f"{n_grants} og.grant rows")

    verdicts = _psql("SELECT count(*) FROM og.verdict WHERE outcome='PASS'")
    n_pass = int(verdicts) if verdicts.isdigit() else 0
    results.record("A6 guardian verdict PASS", n_pass > 0, f"{n_pass} PASS verdicts")

    signed = _psql("SELECT count(*) FROM og.verdict WHERE outcome='PASS' AND signature IS NOT NULL")
    n_signed = int(signed) if signed.isdigit() else 0
    results.record("A6 signed batch", n_signed > 0, f"{n_signed} signed PASS verdicts")


def check_settlement(results: Results) -> None:
    pnl = _psql("SELECT count(*) FROM og.pnl")
    invoices = _psql("SELECT count(*) FROM og.invoice_line")
    n_pnl = int(pnl) if pnl.isdigit() else 0
    n_inv = int(invoices) if invoices.isdigit() else 0
    results.record("A8 settle writes pnl", n_pnl > 0, f"{n_pnl} og.pnl rows")
    results.record("A8 settle writes invoice_line", n_inv > 0, f"{n_inv} og.invoice_line rows")


def check_trace_chain(results: Results, auth: tuple[str, str] | None) -> None:
    status_code, body = _post(f"{API_BASE}/trace/verify", {}, auth=auth)
    if status_code != 200:
        results.record("A9 trace chain verify", False, f"POST /trace/verify -> {status_code}")
        return
    try:
        payload = json.loads(body)
    except json.JSONDecodeError:
        payload = {}
    passed = bool(payload.get("passed"))
    results.record(
        "A9 trace chain verify OK",
        passed,
        f"checked={payload.get('checked')} first_broken={payload.get('first_broken')}",
    )


def check_ui(results: Results) -> None:
    creds = _read_ui_credentials()
    if creds is None:
        results.record("A10 UI reachable", False, f"could not read operator credentials from {CREDS_PATH}")
        return
    status_code, _body = _get(UI_URL, auth=creds, timeout=15)
    results.record("A10 UI 200 with operator credentials", status_code == 200, f"GET {UI_URL} -> {status_code}")


def check_anomaly_and_alert(results: Results) -> None:
    status_code, body = _post(
        f"{CONTROL_BASE}/api/inject",
        {"target": "hub", "type": "hub_offline", "params": {}, "duration_s": 30},
    )
    if status_code not in (200, 201):
        results.record("A11 anomaly injection", False, f"POST /api/inject -> {status_code}: {body[:200]!r}")
        return
    results.record("A11 anomaly injection accepted", True, f"control plane accepted injection: {status_code}")

    alert_seen = False
    for _ in range(15):
        time.sleep(2)
        s, health_body = _get(LOCAL_HEALTH)
        if s == 200:
            try:
                alerts = json.loads(health_body).get("alerts", [])
            except json.JSONDecodeError:
                alerts = []
            if alerts:
                alert_seen = True
                break
    results.record("A11 alert raised after injection", alert_seen, f"alerts present: {alert_seen}")


def main() -> int:
    results = Results()
    auth = _read_ui_credentials()
    check_live_feeds(results)
    check_hubs_online(results, auth)
    check_concurrent_commitments(results)
    check_allocator_guardian_fleet(results)
    check_settlement(results)
    check_trace_chain(results, auth)
    check_ui(results)
    check_anomaly_and_alert(results)

    print("\n".join(results.lines))
    return 0 if results.ok else 1


if __name__ == "__main__":
    raise SystemExit(main())
