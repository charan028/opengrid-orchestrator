"""opengrid.fleet.topology_audit -- the one definition of "an unmapped topology row" (migration 0029 registry).

The guardian raises two warnings for incomplete grid topology (09 S2.6):

* ALR-XFMR-UNMAPPED (`guardian.service._check_transformers`): a hub whose `og.hub.transformer_id` is NULL or
  names no `og.service_transformer` row. G-27 then checks that hub alone at the per-home default rating.
* ALR-BANK-UNMAPPED-TOPOLOGY (`guardian.service._check_unmapped_bank`): a bank with no `og.bank.feeder_id`.
  G-06/G-28/G-32 are then not evaluated for it.

`UNMAPPED_QUERIES` counts the rows behind both, plus the rows that silently fall back to static defaults: a
bank feeder with no `og.feeder_limit` row (G-28 takes `[guardian.flow]`'s defaults) and a home bank with no
HOME_BANK `og.asset` row under a `og.substation_limit` (no G-29 membership). A complete topology has every
count at 0. `dev/seed/topology_seed.py` (verification), `deploy/scripts/bootstrap_check.py` and the DB tests
all read these queries, so the three can never disagree on what "unmapped" means.

A home bank is an `og.bank` row that no SUBSTATION or MOBILE_STORAGE `og.asset` claims: those two classes own a
single-hub bank with a dedicated connection (the substation battery set, a truck at its depot) that is mapped
to its own service transformer but is not a HOME_BANK member of a distribution substation.
"""

from __future__ import annotations

from typing import Any

import psycopg

#: Asset classes that own a dedicated-connection bank (not a HOME_BANK).
DEDICATED_ASSET_CLASSES: tuple[str, ...] = ("SUBSTATION", "MOBILE_STORAGE")

#: The bank ids of the dedicated-connection banks (a subquery; `dev/seed/topology_seed.py` maps them too).
DEDICATED_BANKS_SQL = (
    "SELECT a.bank_id FROM og.asset a WHERE a.bank_id IS NOT NULL AND a.status <> 'RETIRED'"
    " AND a.asset_class IN ('SUBSTATION', 'MOBILE_STORAGE')"
)

#: label -> SELECT count(*) of the rows that are unmapped in that sense. Every count must be 0.
UNMAPPED_QUERIES: dict[str, str] = {
    "hubs without a service transformer (ALR-XFMR-UNMAPPED)": (
        "SELECT count(*) FROM og.hub h WHERE h.transformer_id IS NULL"
        " OR NOT EXISTS (SELECT 1 FROM og.service_transformer st WHERE st.transformer_id = h.transformer_id)"
    ),
    "hubs on a transformer of another bank": (
        "SELECT count(*) FROM og.hub h JOIN og.service_transformer st ON st.transformer_id = h.transformer_id"
        " WHERE st.bank_id IS DISTINCT FROM h.bank_id"
    ),
    "banks without a feeder (ALR-BANK-UNMAPPED-TOPOLOGY)": "SELECT count(*) FROM og.bank WHERE feeder_id IS NULL",
    "bank feeders without og.feeder_limit": (
        "SELECT count(DISTINCT b.feeder_id) FROM og.bank b WHERE b.feeder_id IS NOT NULL"
        " AND NOT EXISTS (SELECT 1 FROM og.feeder_limit f WHERE f.feeder_id = b.feeder_id)"
    ),
    "home banks without a HOME_BANK asset under a substation limit": (
        "SELECT count(*) FROM og.bank b"  # noqa: S608 -- fixed fragments only
        f" WHERE b.bank_id NOT IN ({DEDICATED_BANKS_SQL})"
        " AND NOT EXISTS (SELECT 1 FROM og.asset a JOIN og.substation_limit s ON s.substation_id = a.substation_id"
        " WHERE a.asset_class = 'HOME_BANK' AND a.bank_id = b.bank_id AND a.status <> 'RETIRED')"
    ),
}


def count_unmapped(conn: psycopg.Connection[Any]) -> dict[str, int]:
    """Each `UNMAPPED_QUERIES` count on `conn` (read-only; works inside an open transaction)."""
    counts: dict[str, int] = {}
    for label, query in UNMAPPED_QUERIES.items():
        row = conn.execute(query).fetchone()
        counts[label] = int(row[0]) if row is not None else 0
    return counts
