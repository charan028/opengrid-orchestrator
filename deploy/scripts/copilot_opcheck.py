#!/usr/bin/env python3
"""Post-deploy operational check of the copilot's fleet answers (release r3.4.3). Run as root on base.

Asks the copilot five questions through the real path -- Apache (TLS, Basic Auth as the `viewer`
account) -> og-api `POST /og/api/ai/ask` -- and compares every number in the answers with a direct,
read-only SQL count of the same thing. Prints PASS/FAIL per check; exits 0 only when all pass.

    python3 deploy/scripts/copilot_opcheck.py            # from a checkout, or copy the file anywhere

Secrets: the viewer password is read from /root/opengrid-ui-credentials.txt into memory and sent only
in the Authorization header of a loopback HTTPS request; it is never printed, logged, put on a command
line or written to a file. /etc/opengrid (the Anthropic key in ai_agent.env, the proxy secret) is
neither read nor touched. SQL runs as the postgres OS user with default_transaction_read_only=on.

Each question costs one routing-model screening call (a fraction of a cent against [ai_agent].daily_usd).
Standard library only (Python >= 3.11 for tomllib).
"""

from __future__ import annotations

import base64
import http.client
import json
import math
import os
import re
import ssl
import subprocess
import sys
import tomllib
from datetime import UTC, datetime
from pathlib import Path

CREDENTIALS = Path(os.environ.get("OG_UI_CREDENTIALS", "/root/opengrid-ui-credentials.txt"))
ACCOUNT = os.environ.get("OG_CHECK_ACCOUNT", "viewer")
HOST = os.environ.get("OG_PUBLIC_HOST", "base.tocy-net.net")
CONFIG = Path(os.environ.get("OG_CONFIG", "/opt/opengrid/current/orchestrator/config/orchestrator.toml"))
REGISTRY = CONFIG.parent / "service_profiles" / "mobile_storage_home_stations.toml"
DATABASE = os.environ.get("OG_DB", "og")

#: The D-31 rule as core.geo states it (HOME_STATION_RADIUS_KM, MOBILE_POSITION_MAX_AGE_S): restated here
#: on purpose, so this check is independent of the code it checks.
RADIUS_KM = 0.25
MAX_AGE_S = 300.0

results: list[tuple[str, bool, str]] = []


def password() -> str:
    """The account's password from the credentials file (`<account>: <password>` line). Never printed."""
    for line in CREDENTIALS.read_text(encoding="utf-8").splitlines():
        match = re.match(rf"^{re.escape(ACCOUNT)}\s*:\s*(\S.*?)\s*$", line)
        if match:
            return match.group(1)
    sys.exit(f"no '{ACCOUNT}:' line in {CREDENTIALS}")


def ask(question: str, auth: str) -> dict[str, object]:
    """One copilot question through Apache on loopback (self-signed-safe: loopback only)."""
    context = ssl.create_default_context()
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    conn = http.client.HTTPSConnection("127.0.0.1", 443, context=context, timeout=60)
    body = json.dumps({"question": question, "screen": "/og/opcheck"})
    headers = {
        "Host": HOST,
        "Authorization": auth,
        "Content-Type": "application/json",
        "Accept": "application/json",
    }
    conn.request("POST", "/og/api/ai/ask", body=body, headers=headers)
    response = conn.getresponse()
    raw = response.read().decode("utf-8", "replace")
    conn.close()
    if response.status != 200:
        return {"text": f"HTTP {response.status}: {raw[:200]}", "tier": "error"}
    answer: dict[str, object] = json.loads(raw)
    return answer


def sql(query: str) -> list[list[str]]:
    """A read-only query as the postgres OS user; rows of text fields."""
    env = {**os.environ, "PGOPTIONS": "-c default_transaction_read_only=on"}
    out = subprocess.run(  # noqa: S603 -- fixed argv
        ["runuser", "-u", "postgres", "--", "psql", "-d", DATABASE, "-X", "-At", "-F", "\t", "-c", query],
        capture_output=True,
        text=True,
        check=True,
        env=env,
    ).stdout
    return [line.split("\t") for line in out.splitlines() if line]


def numbers(text: str) -> list[float]:
    return [float(n.replace(",", "")) for n in re.findall(r"\d[\d,]*(?:\.\d+)?", text)]


def record(name: str, ok: bool, detail: str) -> None:
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}: {detail}")


def show(question: str, answer: dict[str, object]) -> str:
    text = str(answer.get("text", ""))
    meta = f"tier={answer.get('tier')} screened_by={answer.get('screened_by')} model={answer.get('model')}"
    print(f"\nQ: {question}\nA: {text}\n   ({meta})")
    return text


def health_thresholds() -> tuple[float, float]:
    """[health].hub_stale_s / hub_offline_s, with HealthThresholds' own defaults."""
    health = tomllib.loads(CONFIG.read_text(encoding="utf-8")).get("health", {})
    return float(health.get("hub_stale_s", 6.0)), float(health.get("hub_offline_s", 30.0))


def km(a: tuple[float, float], b: tuple[float, float]) -> float:
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 2 * 6371.0088 * math.asin(min(1.0, math.sqrt(h)))


# -- the checks ---------------------------------------------------------------------------------------

_JOIN = "FROM og.hub h JOIN og.hub_state s ON s.hub_id = h.hub_id"
_UNAVAILABLE = (
    "coalesce((SELECT to_jsonb(b.*) ->> 'availability' FROM og.bank b WHERE b.bank_id = h.bank_id),"
    " 'AVAILABLE') <> 'AVAILABLE'"
)


def check_count(name: str, question: str, where: str, auth: str) -> None:
    text = show(question, ask(question, auth))
    expected = int(sql(f"SELECT count(*) {_JOIN} WHERE {where}")[0][0])  # noqa: S608 -- fixed text
    got = numbers(text)[:1]
    ok = got == [float(expected)] or (expected == 0 and text.startswith("No "))
    record(name, ok, f"copilot {got[0] if got else text[:40]!r} vs SQL {expected}")


def check_available_kw(auth: str) -> None:
    question = "total available kW in LZ_AEN"
    _stale_s, offline_s = health_thresholds()
    dispatchable = (
        "h.zone = 'LZ_AEN' AND coalesce(s.fault_code, '') = ''"
        " AND lower(coalesce(s.health, '')) NOT IN ('degraded', 'quarantined')"
        f" AND s.last_seen_at >= now() - make_interval(secs => {offline_s}) AND NOT ({_UNAVAILABLE})"
    )
    for attempt in (1, 2):  # health can flip between the two reads; one retry settles it
        text = show(question, ask(question, auth))
        row = sql(f"SELECT coalesce(sum(h.p_kw), 0), count(*) {_JOIN} WHERE {dispatchable}")[0]  # noqa: S608
        kw, hubs = round(float(row[0])), int(row[1])
        got = numbers(text)[:2]
        ok = len(got) == 2 and round(got[0]) == kw and int(got[1]) == hubs
        if ok or attempt == 2:
            record("available kW in LZ_AEN", ok, f"copilot {got} vs SQL [{kw} kW, {hubs} hubs]")
            return


def check_trucks(auth: str) -> None:
    question = "how many trucks are at home"
    text = show(question, ask(question, auth))
    registry = tomllib.loads(REGISTRY.read_text(encoding="utf-8"))
    station = {str(s["home_station_id"]): (float(s["lat"]), float(s["lon"])) for s in registry["home_station"]}
    sites: dict[str, tuple[float, float]] = {}
    mobile: list[str] = []
    for a in registry.get("assignment", []):
        mobile.append(str(a["bank_id"]))
        sites[str(a["bank_id"])] = station[str(a["home_station_id"])]
        if a.get("hub_id"):
            sites[str(a["hub_id"])] = station[str(a["home_station_id"])]
    ids = ",".join("'" + m.replace("'", "''") + "'" for m in mobile) or "''"
    rows = sql(
        "SELECT h.hub_id, h.bank_id, h.device_lat, h.device_lon,"  # noqa: S608 -- ids from the registry
        f" extract(epoch FROM now() - h.device_info_at) {_JOIN}"
        f" WHERE h.bank_id IN ({ids}) OR h.hub_id IN ({ids})"
    )
    home = 0
    for hub_id, bank_id, lat, lon, age in rows:
        site = sites.get(hub_id) or sites.get(bank_id)
        fresh = lat and lon and age and -5.0 <= float(age) <= MAX_AGE_S
        if site and fresh and km((float(lat), float(lon)), site) <= RADIUS_KM:
            home += 1
    got = numbers(text)[:2]
    record(
        "trucks at home (fresh device position)",
        got == [float(home), float(len(rows))],
        f"copilot {got} vs SQL [{home} of {len(rows)}]",
    )


def check_unparseable(auth: str) -> None:
    question = "how many units have more than forty kWh"
    answer = ask(question, auth)
    text = show(question, answer)
    unquoted = re.sub(r"'[^']*'", "", text)
    ok = "couldn't understand" in text and not re.search(r"\d", unquoted.split("Write it")[0])
    ok = ok and answer.get("refusal_reason") == "fleet_condition_unparsed"
    record("unparseable condition gives no count", ok, f"refusal_reason={answer.get('refusal_reason')}")


def main() -> int:
    if os.geteuid() != 0:
        sys.exit("run as root on base")
    auth = "Basic " + base64.b64encode(f"{ACCOUNT}:{password()}".encode()).decode()
    print(f"copilot opcheck {datetime.now(UTC).isoformat(timespec='seconds')} as '{ACCOUNT}' via https://{HOST}")
    check_count("hubs in LZ_NORTH", "how many hubs in LZ_NORTH", "h.zone = 'LZ_NORTH'", auth)
    check_count("hubs rated 78.4 kWh", "how many units have capacity 78.4 kWh", "h.e_kwh BETWEEN 78.35 AND 78.45", auth)
    check_count("hubs on UNAVAILABLE banks", "how many hubs are unavailable", _UNAVAILABLE, auth)
    banks = int(sql("SELECT count(*) FROM og.bank b WHERE coalesce(to_jsonb(b.*) ->> 'availability', 'AVAILABLE') <> 'AVAILABLE'")[0][0])
    record("UNAVAILABLE banks (SQL only, D-37 expects 20)", banks == 20, f"{banks} banks")
    check_available_kw(auth)
    check_trucks(auth)
    check_unparseable(auth)
    failed = [name for name, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} PASS" + (f"; FAIL: {', '.join(failed)}" if failed else ""))
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
