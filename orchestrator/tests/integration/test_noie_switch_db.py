"""D-37 against real Postgres (test cluster only): migration 0046, dev/seed/noie_switch_seed.sql and
deploy/scripts/noie_switch_apply.py -- LCRA/RAYBURN utilities, the two inactive "Sample Contract: ..." tolls,
LZ_LCRA/LZ_RAYBN banks UNAVAILABLE (REGULATED_NO_CONTRACT), idempotency, the checksum guard, and the CHECKs
that forbid an ACTIVE or misnamed sample. Server only: `OG_DB` on the 5433 test cluster."""

from __future__ import annotations

import importlib.util
import os
from pathlib import Path
from typing import Any

import psycopg
import pytest

pytestmark = [
    pytest.mark.skipif(not os.environ.get("OG_DB"), reason="requires the server environment (OG_DB)"),
    pytest.mark.skipif(
        os.environ.get("PGPORT", os.environ.get("OG_DB_PORT", "5432")) == "5432",
        reason="writes og.bank/og.contract rows: test cluster (5433) only",
    ),
]

REPO = Path(__file__).resolve().parents[3]
MARKET_SEED = REPO / "dev" / "seed" / "market_model_seed.sql"
NOIE_SEED = REPO / "dev" / "seed" / "noie_switch_seed.sql"
APPLY = REPO / "deploy" / "scripts" / "noie_switch_apply.py"
TEST_BANKS = {"bank-t37-lcra": "LZ_LCRA", "bank-t37-raybn": "LZ_RAYBN", "bank-t37-north": "LZ_NORTH"}
SAMPLES = ("00000000-0000-7000-8000-00000000ac1d", "00000000-0000-7000-8000-00000000ac2d")


@pytest.fixture(scope="module")
def dsn(server_config, _migrated) -> str:
    from opengrid.platform.db import build_dsn

    dsn = str(build_dsn(server_config))
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(MARKET_SEED.read_text(encoding="utf-8").encode("utf-8"))
        for bank_id, zone in TEST_BANKS.items():
            conn.execute(
                "INSERT INTO og.bank (bank_id, zone, kva_rating) VALUES (%s, %s, 600) ON CONFLICT DO NOTHING",
                (bank_id, zone),
            )
    yield dsn
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute("DELETE FROM og.bank WHERE bank_id = ANY(%s)", (list(TEST_BANKS),))


def _apply_module() -> Any:
    spec = importlib.util.spec_from_file_location("noie_switch_apply", APPLY)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_seed_is_idempotent_and_marks_only_the_switching_zones(dsn: str) -> None:
    sql = NOIE_SEED.read_text(encoding="utf-8").encode("utf-8")
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(sql)
        first = conn.execute(
            "SELECT bank_id, availability, availability_reason, availability_since FROM og.bank"
            " WHERE bank_id = ANY(%s) ORDER BY 1",
            (list(TEST_BANKS),),
        ).fetchall()
        conn.execute(sql)
        second = conn.execute(
            "SELECT bank_id, availability, availability_reason, availability_since FROM og.bank"
            " WHERE bank_id = ANY(%s) ORDER BY 1",
            (list(TEST_BANKS),),
        ).fetchall()
        samples = conn.execute(
            "SELECT name, status, is_sample, utility_id FROM og.contract WHERE contract_id::text = ANY(%s)"
            " ORDER BY name",
            (list(SAMPLES),),
        ).fetchall()
        rules = conn.execute(
            "SELECT min_qty_kw, duration_minutes FROM og.product_rule WHERE contract_id::text = ANY(%s)",
            (list(SAMPLES),),
        ).fetchall()
        utilities = [r[0] for r in conn.execute("SELECT utility_id FROM og.utility ORDER BY 1").fetchall()]
    assert first == second  # idempotent: availability_since not bumped on a re-run
    by_bank = {r[0]: r[1:3] for r in second}
    assert by_bank["bank-t37-lcra"] == ("UNAVAILABLE", "REGULATED_NO_CONTRACT")
    assert by_bank["bank-t37-raybn"] == ("UNAVAILABLE", "REGULATED_NO_CONTRACT")
    assert by_bank["bank-t37-north"] == ("AVAILABLE", None)
    assert samples == [
        ("Sample Contract: LCRA Tolling (placeholder terms)", "SUSPENDED", True, "LCRA"),
        ("Sample Contract: Rayburn Tolling (placeholder terms)", "SUSPENDED", True, "RAYBURN"),
    ]
    assert {(float(k), m) for k, m in rules} == {(6000.0, 90)}
    assert {"LCRA", "RAYBURN"} <= set(utilities)


def test_a_sample_can_never_be_activated_or_misnamed(dsn: str) -> None:
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(NOIE_SEED.read_text(encoding="utf-8").encode("utf-8"))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE og.contract SET status = 'ACTIVE' WHERE contract_id = %s", (SAMPLES[0],))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute("UPDATE og.contract SET name = 'LCRA toll' WHERE contract_id = %s", (SAMPLES[0],))
        with pytest.raises(psycopg.errors.CheckViolation):
            conn.execute(
                "UPDATE og.bank SET availability = 'UNAVAILABLE', availability_reason = NULL"
                " WHERE bank_id = 'bank-t37-north'"
            )


def test_apply_script_dry_run_changes_nothing_and_guard_passes(
    dsn: str, capsys: pytest.CaptureFixture[str]
) -> None:
    apply = _apply_module()
    with psycopg.connect(dsn, autocommit=True) as conn:
        conn.execute(
            "UPDATE og.bank SET availability = 'AVAILABLE', availability_reason = NULL, availability_since = NULL"
            " WHERE bank_id = 'bank-t37-lcra'"
        )
    assert apply.main(["--dsn", dsn]) == 0
    out = capsys.readouterr().out
    assert "dry run (rolled back)" in out and "guarded rows unchanged: OK" in out
    with psycopg.connect(dsn, autocommit=True) as conn:
        (state,) = conn.execute("SELECT availability FROM og.bank WHERE bank_id = 'bank-t37-lcra'").fetchone()
    assert state == "AVAILABLE"  # dry run rolled back
    assert apply.main(["--dsn", dsn, "--apply"]) == 0
    assert apply.main(["--dsn", dsn, "--apply"]) == 0  # idempotent
    with psycopg.connect(dsn, autocommit=True) as conn:
        (state,) = conn.execute("SELECT availability FROM og.bank WHERE bank_id = 'bank-t37-lcra'").fetchone()
    assert state == "UNAVAILABLE"
