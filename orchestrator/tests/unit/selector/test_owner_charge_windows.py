"""Owner decision D-30: the owner's grid-charging windows for toll banks, editable on the Fleet page
(`og.owner_charge_window`), most specific scope wins, then config, then the built-in 22:00-06:00."""

from __future__ import annotations

import pytest

from opengrid.selector import gate


@pytest.fixture(autouse=True)
def _fresh_cache():
    gate.clear_owner_charge_windows_cache()
    yield
    gate.clear_owner_charge_windows_cache()


def test_most_specific_scope_wins():
    scoped = {
        ("FLEET", "*"): ["20:00-23:00"],
        ("PROVIDER", "AUSTIN_ENERGY"): ["21:00-05:00"],
        ("ZONE", "LZ_AEN"): ["22:00-04:00"],
        ("FEEDER", "f-7"): ["23:00-03:00"],
        ("BANK", "b-1"): ["00:00-02:00"],
    }

    def resolve(bank: str, feeder: str | None, zone: str | None, provider: str | None):
        return gate.resolve_owner_charge_windows(
            scoped, bank_id=bank, feeder=feeder, zone=zone, provider=provider
        )

    assert resolve("b-1", "f-7", "LZ_AEN", "AUSTIN_ENERGY") == ["00:00-02:00"]
    assert resolve("b-2", "f-7", "LZ_AEN", "AUSTIN_ENERGY") == ["23:00-03:00"]
    assert resolve("b-2", None, "LZ_AEN", "AUSTIN_ENERGY") == ["22:00-04:00"]
    assert resolve("b-2", None, "LZ_CPS", "AUSTIN_ENERGY") == ["21:00-05:00"]
    assert resolve("b-2", None, None, "CPS_ENERGY") == ["20:00-23:00"]
    assert gate.resolve_owner_charge_windows({}, bank_id="b", feeder=None, zone=None, provider=None) is None


def test_substation_and_hub_scopes_follow_the_shared_resolver():
    """`core.charge_windows` precedence: HUB > BANK > FEEDER > SUBSTATION > ZONE, and an empty list is a
    valid "no grid charging" override (not a missing row)."""
    scoped = {
        ("ZONE", "LZ_AEN"): ["22:00-04:00"],
        ("SUBSTATION", "sub-9"): ["23:00-05:00"],
        ("HUB", "trailer-mb-01"): [],
    }
    common = {"feeder": None, "zone": "LZ_AEN", "provider": "AUSTIN_ENERGY"}
    assert gate.resolve_owner_charge_windows(scoped, bank_id="b-1", substation="sub-9", **common) == [
        "23:00-05:00"
    ]
    assert gate.resolve_owner_charge_windows(scoped, bank_id="b-1", **common) == ["22:00-04:00"]
    assert (
        gate.resolve_owner_charge_windows(
            scoped, bank_id="trailer-mb-01", hub_id="trailer-mb-01", substation="sub-9", **common
        )
        == []
    )


async def test_db_rows_override_config_and_are_cached(monkeypatch):
    calls: list[int] = []

    async def _rows():
        calls.append(1)
        return {("PROVIDER", "AUSTIN_ENERGY"): ["01:00-05:00"]}

    monkeypatch.setattr(gate.db, "load_owner_charge_window_rows", _rows)
    monkeypatch.setattr(
        gate,
        "config_owner_charge_windows",
        lambda: {"AUSTIN_ENERGY": ["22:00-06:00"], "bank-000": ["02:00-03:00"]},
    )

    first = await gate.load_owner_charge_windows()
    second = await gate.load_owner_charge_windows()

    assert first[("PROVIDER", "AUSTIN_ENERGY")] == ["01:00-05:00"]  # the Fleet-page edit wins
    assert first[("BANK", "bank-000")] == ["02:00-03:00"]  # config still fills what the table lacks
    assert second is first and calls == [1]  # re-read at most every 60 s


async def test_missing_table_falls_back_to_config_then_the_default(monkeypatch):
    async def _no_table():
        raise RuntimeError('relation "og.owner_charge_window" does not exist')

    monkeypatch.setattr(gate.db, "load_owner_charge_window_rows", _no_table)
    monkeypatch.setattr(gate, "config_owner_charge_windows", dict)

    assert await gate.load_owner_charge_windows() == {}
    assert gate.DEFAULT_OWNER_CHARGE_WINDOWS == ("22:00-06:00",)
