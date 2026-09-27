"""D-29: the 20 MW Austin substation set's guardian limits (seed + orchestrator.toml) let a 20 MW firm toll
call through G-06 (feeder ramp ceiling), G-28 (feeder thermal/reverse) and G-29 (substation limit), on a
feeder of its own so the Austin home banks keep the defaults."""

from __future__ import annotations

import re
from pathlib import Path

from opengrid.guardian.config import load_guardian_config
from opengrid.platform.config import load_config

ORCH = Path(__file__).resolve().parents[3]
SEED = ORCH.parent / "dev" / "seed" / "market_model_seed.sql"
FEEDER = "feeder-sub-LZ_AEN-00"
TOLL_KW = 20_000.0


def test_guardian_ramp_ceiling_for_the_substation_feeder_only() -> None:
    config = load_guardian_config(load_config(ORCH / "config" / "orchestrator.toml"))
    ceiling = config.feeder_ramp_ceiling_kw_per_min[FEEDER]
    assert ceiling >= TOLL_KW  # full 20 MW within one minute
    # The Austin home feeder carries the toll's 10 x 600 kW home-bank share: 7 MW/min covers its ~6.7 MW/min ramp.
    assert config.feeder_ramp_ceiling_kw_per_min["feeder-LZ_AEN-00"] == 7000
    # every other home feeder keeps the 3 MW/min default
    assert set(config.feeder_ramp_ceiling_kw_per_min) == {FEEDER, "feeder-LZ_AEN-00"}


def test_seed_limits_cover_a_20_mw_call_on_its_own_feeder() -> None:
    sql = SEED.read_text(encoding="utf-8")
    feeder = re.search(r"INTO og\.feeder_limit .*?VALUES \('([^']+)', (\d+), (\d+)\)", sql, re.S)
    assert feeder is not None
    feeder_id, thermal, reverse = feeder.group(1), float(feeder.group(2)), float(feeder.group(3))
    assert feeder_id == FEEDER
    assert 0.95 * thermal >= TOLL_KW  # G-28 applies feeder_thermal_pct (0.95)
    assert reverse >= TOLL_KW
    substation = re.search(r"INTO og\.substation_limit .*?VALUES \('([^']+)', (\d+), (\d+)\)", sql, re.S)
    assert substation is not None and substation.group(1) == "sub-LZ_AEN-00"
    assert float(substation.group(2)) >= TOLL_KW and float(substation.group(3)) >= TOLL_KW
    bank = re.search(r"VALUES \('bank-sub-LZ_AEN-00', 'LZ_AEN', (\d+), 0, '([^']+)'\)", sql)
    assert bank is not None and float(bank.group(1)) >= TOLL_KW and bank.group(2) == FEEDER
    assert (
        "VALUES ('sub-LZ_AEN-00', 'bank-sub-LZ_AEN-00', 'LZ_AEN', 40000, 8000, 20000, 0.9381, 0.9381)" in sql
    )
