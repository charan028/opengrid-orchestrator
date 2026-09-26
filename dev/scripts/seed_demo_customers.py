"""Idempotent demo seed: the three committed customers `docs/demo/README.md` (DEMO-2) needs.

It offers the three seeded demo contracts (`orchestrator/migrations/0002_seed_demo.sql`: ERCOT_ENERGY `...0d02`,
DIST_DEFERRAL `...0d04`, PARTNER_CAPACITY `...0d05`) for one demo window and lets the selector commit them:

- each opportunity goes through og-api's admission (`POST /og/api/opportunities`), exactly as an operator's;
- its value is set in the database right after admission, because the API has no field for it (the same
  step `tests-e2e/functional` takes);
- the selector's own gate commits it. The script never writes obligations, commitments or reservations: the
  ledger has one writer (K2), and a demo must show the real flow.

The selector decides which banks carry each obligation, so the script waits for the commit and prints them.
Use the DIST_DEFERRAL obligation's bank as the demo bank (DEMO-2 steps 6 and 11-12 assume `bank-012`).

Idempotent: a contract that already has a live obligation (SELECTED, COMMITTED or DELIVERING) overlapping the
window is left alone, and an OFFERED opportunity for the same window is reused instead of admitted twice.

Dev stack (reads dev/secrets and dev/.env like tests-e2e/functional; prints no secret):

    python dev/scripts/seed_demo_customers.py [--start 2026-09-26T22:00:00Z] [--minutes 60]

Server (only with the lead's OK, run by the release manager): set OG_SEED_API (og-api on loopback),
OG_SEED_DSN and OG_API_PROXY_SECRET in the environment of the run. The script reads no file under /etc.
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import httpx
import psycopg
from psycopg.rows import dict_row

DEV_DIR = Path(__file__).resolve().parents[1]
INTERVAL = timedelta(minutes=15)
LIVE_STATES = ("SELECTED", "COMMITTED", "DELIVERING")

#: (label, contract id from 0002_seed_demo.sql, requested kW). PARTNER_CAPACITY is an all-or-nothing block
#: with a 50 kW minimum (its product rule `...0e05`).
DEMO_CUSTOMERS = (
    ("ERCOT_ENERGY", "00000000-0000-7000-8000-000000000d02", 40.0),
    ("DIST_DEFERRAL", "00000000-0000-7000-8000-000000000d04", 60.0),
    ("PARTNER_CAPACITY", "00000000-0000-7000-8000-000000000d05", 50.0),
)


def _dev_value(path: Path, key: str, default: str = "") -> str:
    if path.is_file():
        for line in path.read_text(encoding="utf-8").splitlines():
            name, sep, value = line.partition("=")
            if sep and name.strip() == key and value.strip():
                return value.strip()
    return default


def _dsn() -> str:
    explicit = os.environ.get("OG_SEED_DSN")
    if explicit:
        return explicit
    password = _dev_value(DEV_DIR / "secrets", "OG_DB_PASSWORD")
    port = _dev_value(DEV_DIR / ".env", "POSTGRES_PORT", "5432")
    if not password:
        raise SystemExit(
            "seed: set OG_SEED_DSN, or create dev/secrets (see dev/secrets.example)"
        )
    return f"postgresql://opengrid:{password}@127.0.0.1:{port}/og"


def _headers() -> dict[str, str]:
    secret = os.environ.get("OG_API_PROXY_SECRET") or _dev_value(
        DEV_DIR / "secrets", "OG_API_PROXY_SECRET"
    )
    if not secret:
        raise SystemExit("seed: set OG_API_PROXY_SECRET, or create dev/secrets")
    return {
        "X-Remote-User": os.environ.get("OG_SEED_USER", "operator"),
        "X-OG-Proxy-Auth": secret,
    }


def quarter(offset: int, *, at: datetime | None = None) -> datetime:
    at = at or datetime.now(UTC)
    floored = at.replace(minute=at.minute - at.minute % 15, second=0, microsecond=0)
    return floored + INTERVAL * offset


def _rows(
    conn: psycopg.Connection[Any], sql: str, params: dict[str, Any]
) -> list[dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(sql, params)
        return list(cur.fetchall())


def _live_obligation(
    conn: psycopg.Connection[Any], contract_id: str, start: datetime, end: datetime
) -> Any:
    found = _rows(
        conn,
        """SELECT obligation_id, opportunity_id, state FROM og.obligation
           WHERE contract_id = %(c)s AND state = ANY(%(s)s)
             AND window_start < %(e)s AND window_end > %(b)s
           ORDER BY window_start LIMIT 1""",
        {"c": contract_id, "s": list(LIVE_STATES), "b": start, "e": end},
    )
    return found[0] if found else None


def _offered(
    conn: psycopg.Connection[Any], contract_id: str, start: datetime, end: datetime
) -> Any:
    found = _rows(
        conn,
        """SELECT opportunity_id FROM og.opportunity
           WHERE contract_id = %(c)s AND state = 'OFFERED' AND window_start = %(b)s AND window_end = %(e)s
           LIMIT 1""",
        {"c": contract_id, "b": start, "e": end},
    )
    return found[0]["opportunity_id"] if found else None


def _banks(conn: psycopg.Connection[Any], opportunity_id: Any) -> list[str]:
    found = _rows(
        conn,
        """SELECT DISTINCT r.bank_id::text AS bank_id FROM og.reservation r
           JOIN og.obligation o USING (obligation_id)
           WHERE o.opportunity_id = %(o)s AND r.released_at IS NULL ORDER BY 1""",
        {"o": opportunity_id},
    )
    return [row["bank_id"] for row in found]


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "--start",
        help="window start, ISO UTC on a quarter hour (default: the quarter after next)",
    )
    parser.add_argument(
        "--minutes", type=int, default=60, help="window length in minutes (default 60)"
    )
    parser.add_argument(
        "--value", type=float, default=180.0, help="offer value in $/MWh (default 180)"
    )
    parser.add_argument(
        "--wait",
        type=float,
        default=180.0,
        help="seconds to wait for the commit (default 180)",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="report what would be done, change nothing",
    )
    args = parser.parse_args()

    start = (
        datetime.fromisoformat(args.start.replace("Z", "+00:00"))
        if args.start
        else quarter(2)
    )
    end = start + timedelta(minutes=args.minutes)
    api = os.environ.get("OG_SEED_API", "http://127.0.0.1:8080/og/api").rstrip("/")
    print(f"demo window {start:%Y-%m-%d %H:%M}Z - {end:%H:%M}Z")

    offered: list[tuple[str, Any]] = []
    with (
        psycopg.connect(_dsn(), autocommit=True) as conn,
        httpx.Client(timeout=30.0) as http,
    ):
        for label, contract_id, kw in DEMO_CUSTOMERS:
            live = _live_obligation(conn, contract_id, start, end)
            if live:
                print(
                    f"{label}: already {live['state']} (obligation {live['obligation_id']}), left alone"
                )
                offered.append((label, live["opportunity_id"]))
                continue
            opportunity_id = _offered(conn, contract_id, start, end)
            if opportunity_id is None:
                if args.dry_run:
                    print(f"{label}: would offer {kw:.0f} kW")
                    continue
                resp = http.post(
                    f"{api}/opportunities",
                    headers=_headers(),
                    json={
                        "contract_id": contract_id,
                        "window_start": start.isoformat(),
                        "window_end": end.isoformat(),
                        "requested_kw": str(kw),
                    },
                )
                if resp.status_code != 201:
                    print(
                        f"{label}: admission refused ({resp.status_code}): {resp.text}"
                    )
                    continue
                opportunity_id = resp.json()["opportunity_id"]
                print(f"{label}: offered {kw:.0f} kW (opportunity {opportunity_id})")
            else:
                print(f"{label}: reusing OFFERED opportunity {opportunity_id}")
            if not args.dry_run:
                with conn.cursor() as cur:
                    cur.execute(
                        "UPDATE og.opportunity SET value_per_mwh = %(v)s WHERE opportunity_id = %(o)s",
                        {"v": args.value, "o": opportunity_id},
                    )
            offered.append((label, opportunity_id))

        if args.dry_run:
            return 0
        deadline = time.monotonic() + args.wait
        pending = dict(offered)
        states: dict[str, str] = {}
        while pending and time.monotonic() < deadline:
            for label, opportunity_id in list(pending.items()):
                found = _rows(
                    conn,
                    "SELECT state FROM og.obligation WHERE opportunity_id = %(o)s",
                    {"o": opportunity_id},
                )
                state = found[0]["state"] if found else "OFFERED"
                states[label] = state
                if state in ("COMMITTED", "DELIVERING"):
                    del pending[label]
            if pending:
                time.sleep(2)

        ok = True
        carried: dict[str, list[str]] = {}
        for label, opportunity_id in offered:
            state = states.get(label, "unknown")
            banks = _banks(conn, opportunity_id)
            ok = ok and state in ("COMMITTED", "DELIVERING")
            print(f"{label}: {state}; banks {', '.join(banks) or 'none yet'}")
            for bank in banks:
                carried.setdefault(bank, []).append(label)
        if carried:
            # The demo bank: the one carrying the most demo customers, preferring one with the DIST_DEFERRAL
            # (its PI loop is what the overload step shows), then the lowest id.
            bank = min(
                carried,
                key=lambda b: (-len(carried[b]), "DIST_DEFERRAL" not in carried[b], b),
            )
            zone = _rows(
                conn, "SELECT zone FROM og.bank WHERE bank_id = %(b)s", {"b": bank}
            )
            first_hub = _rows(
                conn,
                "SELECT min(hub_id) AS hub_id FROM og.hub WHERE bank_id = %(b)s",
                {"b": bank},
            )
            print(
                f"demo bank: {bank} (zone {zone[0]['zone'] if zone else '?'}; carries "
                f"{', '.join(carried[bank])}); a hub on it: {first_hub[0]['hub_id'] if first_hub else '?'}"
            )
        if not ok:
            print(
                "not every demo customer is committed yet; the selector's next gate may still take them"
            )
        return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
