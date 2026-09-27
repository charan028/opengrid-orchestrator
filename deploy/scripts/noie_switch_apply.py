#!/usr/bin/env python3
"""deploy/scripts/noie_switch_apply.py -- D-37 production data for r3.4.1 (release manager applies it).

Runs dev/seed/noie_switch_seed.sql (the same file a fresh bootstrap applies) in ONE transaction:
LCRA/RAYBURN og.utility rows, the two inactive "Sample Contract: ..." tolls, og.bank availability
UNAVAILABLE / REGULATED_NO_CONTRACT for LZ_LCRA/LZ_RAYBN banks, and their HOME_BANK asset utility.

Safety (dry-run by DEFAULT; --apply to commit):
  * Checksum guard: every og.bank / og.hub / og.asset / og.contract row of the untouched zones and contracts
    (AEN, CPS, NORTH, SOUTH, HOUSTON, WEST, trucks, substation set) must be byte-identical before and after.
  * Obligations, reservations, commitments and grants are never modified: their fingerprints (live rows)
    must be identical too; any difference rolls back (K13).
  * Idempotent: a second run changes nothing.

Preconditions: migration 0046 applied; the new orchestrator config (tdsp_tariffs.toml) deployed with it.
Order in the deploy: migrations -> this script (--apply) -> topology_backfill.sh -> restart services.

    OG_DSN=... python deploy/scripts/noie_switch_apply.py            # dry run: plan + rollback
    OG_DSN=... python deploy/scripts/noie_switch_apply.py --apply    # commit
"""

from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
SEED = REPO_ROOT / "dev" / "seed" / "noie_switch_seed.sql"
SWITCH_ZONES = ("LZ_LCRA", "LZ_RAYBN")
SAMPLE_CONTRACTS = ("00000000-0000-7000-8000-00000000ac1d", "00000000-0000-7000-8000-00000000ac2d")

#: Rows that must not change: table -> SELECT (key, row text).
GUARDED: dict[str, str] = {
    "og.bank (other zones)": "SELECT bank_id, to_jsonb(b)::text FROM og.bank b WHERE zone <> ALL(%(z)s)",
    "og.hub (other zones)": "SELECT hub_id, to_jsonb(h)::text FROM og.hub h WHERE zone <> ALL(%(z)s)",
    "og.hub (switching zones)": "SELECT hub_id, to_jsonb(h)::text FROM og.hub h WHERE zone = ANY(%(z)s)",
    "og.asset (other zones)": "SELECT asset_id, to_jsonb(a)::text FROM og.asset a WHERE zone <> ALL(%(z)s)",
    "og.contract (not the samples)": (
        "SELECT contract_id::text, to_jsonb(c)::text FROM og.contract c WHERE contract_id::text <> ALL(%(s)s)"
    ),
    "og.utility AUSTIN_ENERGY/CPS_ENERGY": (
        "SELECT utility_id, to_jsonb(u)::text FROM og.utility u WHERE utility_id IN ('AUSTIN_ENERGY','CPS_ENERGY')"
    ),
    "og.obligation (live)": (
        "SELECT obligation_id::text, to_jsonb(o)::text FROM og.obligation o "
        "WHERE state IN ('OFFERED','SELECTED','COMMITTED','DELIVERING','SHORTFALL')"
    ),
    "og.reservation (live)": (
        "SELECT reservation_id::text, to_jsonb(r)::text FROM og.reservation r "
        "WHERE released_at IS NULL AND interval_end > now()"
    ),
}

COUNTS = """
SELECT 'utilities LCRA/RAYBURN', count(*) FROM og.utility WHERE utility_id IN ('LCRA','RAYBURN')
UNION ALL SELECT 'sample contracts (SUSPENDED)', count(*) FROM og.contract
  WHERE is_sample AND status = 'SUSPENDED' AND name LIKE 'Sample Contract%%'
UNION ALL SELECT 'banks UNAVAILABLE REGULATED_NO_CONTRACT', count(*) FROM og.bank
  WHERE availability = 'UNAVAILABLE' AND availability_reason = 'REGULATED_NO_CONTRACT'
UNION ALL SELECT 'switching-zone banks still AVAILABLE', count(*) FROM og.bank
  WHERE zone = ANY(%(z)s) AND availability = 'AVAILABLE'
UNION ALL SELECT 'HOME_BANK assets LCRA/RAYBURN', count(*) FROM og.asset
  WHERE asset_class = 'HOME_BANK' AND utility_id IN ('LCRA','RAYBURN')
UNION ALL SELECT 'HOME_BANK assets in switching zones without utility', count(*) FROM og.asset
  WHERE asset_class = 'HOME_BANK' AND zone = ANY(%(z)s) AND utility_id IS NULL
"""


def _snapshot(conn: Any) -> dict[str, dict[str, str]]:
    params = {"z": list(SWITCH_ZONES), "s": list(SAMPLE_CONTRACTS)}
    out: dict[str, dict[str, str]] = {}
    for name, sql in GUARDED.items():
        rows = conn.execute(sql, params).fetchall()
        out[name] = {str(k): str(v) for k, v in rows}
    return out


def _changed(before: dict[str, dict[str, str]], after: dict[str, dict[str, str]]) -> list[str]:
    return [n for n, rows in before.items() if rows != after.get(n)]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter
    )
    parser.add_argument("--dsn", default=os.environ.get("OG_DSN"), help="default: $OG_DSN")
    parser.add_argument("--apply", action="store_true", help="commit (default: dry run, rolled back)")
    args = parser.parse_args(argv)
    if not args.dsn:
        parser.error("--dsn or OG_DSN is required")
    import psycopg

    sql = SEED.read_text(encoding="utf-8")
    params = {"z": list(SWITCH_ZONES)}
    with psycopg.connect(args.dsn) as conn:
        try:
            conn.execute("SET TRANSACTION ISOLATION LEVEL REPEATABLE READ")
            conn.execute("SET LOCAL lock_timeout = '5s'")
            conn.execute("SET LOCAL statement_timeout = '60s'")
            before_counts = conn.execute(COUNTS, params).fetchall()
            before = _snapshot(conn)
            conn.execute(sql)  # type: ignore[arg-type]  # a trusted repo file, many statements
            after = _snapshot(conn)
            after_counts = conn.execute(COUNTS, params).fetchall()
            changed = _changed(before, after)
            if changed:
                raise RuntimeError(f"guarded rows would change: {', '.join(changed)} -- rolled back")
            print("dry run (rolled back):" if not args.apply else "applied:")
            for (label, n0), (_, n1) in zip(before_counts, after_counts, strict=True):
                print(f"  {label}: {n0} -> {n1}")
            print(f"  guarded rows unchanged: OK ({len(GUARDED)} sets: {', '.join(GUARDED)})")
            if args.apply:
                conn.commit()
            else:
                conn.rollback()
        except BaseException:
            conn.rollback()
            raise
    return 0


if __name__ == "__main__":
    sys.exit(main())
