"""`MarketModel` read API (09 S2.1/S2.3 eligibility, C25; TS-19-17) and its config loading."""

from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from opengrid.core.models.market import Asset
from opengrid.market import FREE, MarketModelError, MarketRef, load_market_model
from opengrid.market.config import (
    AUSTIN_ENERGY,
    DEFAULT_UTILITIES,
    load_zone_owners,
    load_zone_territory,
    parse_zone_owners,
    parse_zone_territory,
)
from opengrid.market.model import MarketModel

CT = ZoneInfo("America/Chicago")
CONFIG_DIR = Path(__file__).resolve().parents[3] / "config"
OG_CONFIG = CONFIG_DIR / "orchestrator.toml"  # only its directory is used to find tdsp_tariffs.toml

BANKS = [("bank-000", "LZ_NORTH"), ("bank-010", "LZ_HOUSTON"), ("bank-040", "LZ_AEN"), ("bank-050", "LZ_CPS")]
SUB = Asset(
    asset_id="sub-aen-01",
    asset_class="SUBSTATION",
    feeder_id="feeder-LZ_AEN-00",
    substation_id="sub-aen-01",
    zone="LZ_AEN",
    utility_id="AUSTIN_ENERGY",
    p_kw=Decimal("20000"),
    e_kwh=Decimal("40000"),
    eta_rt=Decimal("0.88"),
    poi_import_kva=Decimal("20000"),
    poi_export_kva=Decimal("20000"),
)


@pytest.fixture
def model() -> MarketModel:
    return load_market_model(banks=BANKS, assets=[SUB], config_path=OG_CONFIG)


def test_zone_territory_loads_from_the_repo_config() -> None:
    assert load_zone_territory(CONFIG_DIR / "tdsp_tariffs.toml") == {
        "LZ_AEN": "AUSTIN_ENERGY",
        "LZ_CPS": "CPS_ENERGY",
    }


def test_zone_territory_rejects_unknown_utility() -> None:
    with pytest.raises(ValueError, match="unknown utility"):
        parse_zone_territory({"LZ_X": {"utility": "NOPE", "market": "REGULATED"}})
    assert parse_zone_territory({"LZ_Y": {"utility": "NOPE", "market": "FREE"}}) == {}


def test_territory_bank_ids(model: MarketModel) -> None:
    assert model.territory_bank_ids("AUSTIN_ENERGY") == {"bank-040"}
    assert model.territory_bank_ids("CPS_ENERGY") == {"bank-050"}
    assert model.territory_asset_ids("AUSTIN_ENERGY") == {"sub-aen-01"}
    assert model.territory_of_bank("bank-000") == "ERCOT_COMPETITIVE"
    assert model.territory_of_bank("bank-999") is None


def test_eligibility_is_territory_bound(model: MarketModel) -> None:
    """TS-19-17: REG eligible set is a subset of the territory; FREE excludes no-access territories."""
    assert model.eligible_bank_ids(MarketRef("REGULATED", "AUSTIN_ENERGY")) == {"bank-040"}
    assert model.eligible_bank_ids(FREE) == {"bank-000", "bank-010"}
    assert model.eligible_asset_ids(MarketRef("REGULATED", "AUSTIN_ENERGY")) == {"sub-aen-01"}
    assert model.eligible_asset_ids(FREE) == frozenset()
    assert not model.bank_eligible("bank-999", FREE)


def test_free_access_flag_opens_territory_to_free() -> None:
    open_ae = AUSTIN_ENERGY.model_copy(update={"free_access_granted": True})
    model = load_market_model(
        banks=BANKS, utilities=[open_ae, DEFAULT_UTILITIES["CPS_ENERGY"]], config_path=OG_CONFIG
    )
    assert model.eligible_bank_ids(FREE) == {"bank-000", "bank-010", "bank-040"}


def test_asset_with_contradicting_utility_is_unknown() -> None:
    wrong = SUB.model_copy(update={"asset_id": "bad", "utility_id": "CPS_ENERGY"})
    model = load_market_model(banks=BANKS, assets=[wrong], config_path=OG_CONFIG)
    assert model.territory_of_asset("bad") is None


def test_charging_cost_by_zone(model: MarketModel) -> None:
    night = datetime(2026, 9, 28, 3, 0, tzinfo=CT)
    ae = model.charging_cost("LZ_AEN", night)
    assert ae.market == "REGULATED" and ae.delivery_usd_per_kwh == 0
    assert ae.blended_usd_per_kwh == Decimal("0.0307390")
    north = model.charging_cost("LZ_NORTH", night, wholesale_usd_per_kwh=Decimal("0.02"))
    assert north.market == "FREE"
    assert north.delivery_usd_per_kwh == Decimal("0.060295")  # Oncor M1 from tdsp_tariffs.toml
    assert north.blended_usd_per_kwh == Decimal("0.080295")


def test_charging_cost_fails_closed(model: MarketModel) -> None:
    night = datetime(2026, 9, 28, 3, 0, tzinfo=CT)
    with pytest.raises(MarketModelError):
        model.charging_cost("LZ_NORTH", night)  # no wholesale price
    with pytest.raises(MarketModelError):
        model.charging_cost("HB_BUSAVG", night)
    no_cps = MarketModel(zone_territory={"LZ_CPS": "CPS_ENERGY"}, utilities={}, banks=[])
    with pytest.raises(MarketModelError):
        no_cps.charging_cost("LZ_CPS", night)


# --- NOIE zones --------------------------------------------------------------------------------------

_NOIE_TOML = """
[[tariff]]
tdsp = "ONCOR"
effective_from = "2026-09-01"
volumetric_usd_per_kwh = 0.060295
load_zones = ["LZ_NORTH"]

[zone_default_tdsp]
LZ_NORTH = "ONCOR"

[zone_territory]
LZ_AEN = { utility = "AUSTIN_ENERGY", market = "REGULATED", delivery_charge = "NONE" }
LZ_LCRA = { utility = "LCRA", market = "NOIE", delivery_charge = "NONE" }
LZ_RAYBN = { utility = "RAYBURN_COUNTRY_EC", market = "FREE", delivery_charge = "NONE" }
"""


@pytest.fixture
def noie_model(tmp_path: Path) -> MarketModel:
    (tmp_path / "tdsp_tariffs.toml").write_text(_NOIE_TOML, encoding="utf-8")
    banks = [
        ("bank-000", "LZ_NORTH"),
        ("bank-040", "LZ_AEN"),
        ("bank-060", "LZ_LCRA"),
        ("bank-070", "LZ_RAYBN"),
    ]
    return load_market_model(banks=banks, config_path=tmp_path / "orchestrator.toml")


def test_zone_owners_include_noie_and_regulated_map_does_not(tmp_path: Path) -> None:
    (tmp_path / "t.toml").write_text(_NOIE_TOML, encoding="utf-8")
    assert load_zone_owners(tmp_path / "t.toml") == {"LZ_AEN": "AUSTIN_ENERGY", "LZ_LCRA": "NOIE"}
    assert load_zone_territory(tmp_path / "t.toml") == {"LZ_AEN": "AUSTIN_ENERGY"}


def test_zone_owners_reject_unknown_market_label() -> None:
    with pytest.raises(ValueError, match="unknown market"):
        parse_zone_owners({"LZ_X": {"utility": "X", "market": "NOIE_TYPO"}})


def test_repo_config_has_no_noie_zone_yet() -> None:
    """LCRA/RAYBN stay FREE until the owner decides (issue #36 point 4): nothing changes today."""
    owners = load_zone_owners(CONFIG_DIR / "tdsp_tariffs.toml")
    assert "NOIE" not in owners.values()


def test_noie_bank_serves_neither_market(noie_model: MarketModel) -> None:
    assert noie_model.territory_of_bank("bank-060") == "NOIE"
    assert noie_model.territory_of_bank("bank-070") == "ERCOT_COMPETITIVE"  # FREE label: competitive
    assert noie_model.eligible_bank_ids(FREE) == {"bank-000", "bank-070"}
    assert noie_model.eligible_bank_ids(MarketRef("REGULATED", "AUSTIN_ENERGY")) == {"bank-040"}
    assert not noie_model.free_access("NOIE")


def test_noie_charging_cost_is_an_explicit_error(noie_model: MarketModel) -> None:
    with pytest.raises(MarketModelError, match="NOIE"):
        noie_model.charging_cost(
            "LZ_LCRA", datetime(2026, 9, 28, 3, 0, tzinfo=CT), wholesale_usd_per_kwh=Decimal("0.02")
        )
