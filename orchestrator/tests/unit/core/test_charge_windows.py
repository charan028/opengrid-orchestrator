"""D-30 charge windows: format, midnight wrap, and most-specific-scope resolution (core, shared)."""

from __future__ import annotations

from datetime import time

import pytest

from opengrid.core.charge_windows import (
    ChargeWindowError,
    Topology,
    in_windows,
    parse_window,
    resolve,
    validate_scope,
    validate_windows,
)


def test_parse_and_validate() -> None:
    assert parse_window("22:00-06:00") == (time(22), time(6))
    assert validate_windows([" 01:00-05:30 ", "13:00-15:00"]) == ["01:00-05:30", "13:00-15:00"]
    assert validate_windows([]) == []  # a valid override: no grid charging
    for bad in (["24:00-01:00"], ["7:00-9:00"], ["10:00-10:00"], ["10:00 - 11:00"], ["a-b"]):
        with pytest.raises(ChargeWindowError):
            validate_windows(bad)
    with pytest.raises(ChargeWindowError, match="at most 4"):
        validate_windows(["00:00-01:00", "02:00-03:00", "04:00-05:00", "06:00-07:00", "08:00-09:00"])
    with pytest.raises(ChargeWindowError, match="duplicate"):
        validate_windows(["00:00-01:00", "00:00-01:00"])


def test_scope_validation() -> None:
    assert validate_scope("FLEET", "*") == "FLEET"
    assert validate_scope("PROVIDER", "ONCOR") == "PROVIDER"
    with pytest.raises(ChargeWindowError):
        validate_scope("FLEET", "all")
    with pytest.raises(ChargeWindowError):
        validate_scope("COUNTY", "Travis")
    with pytest.raises(ChargeWindowError):
        validate_scope("BANK", " ")


def test_windows_wrap_midnight_with_an_exclusive_end() -> None:
    windows = ["22:00-06:00"]
    assert (
        in_windows(windows, time(23, 30))
        and in_windows(windows, time(0))
        and in_windows(windows, time(5, 59))
    )
    assert not in_windows(windows, time(6)) and not in_windows(windows, time(12))
    assert in_windows(["13:00-15:00"], time(13)) and not in_windows(["13:00-15:00"], time(15))
    assert not in_windows([], time(1))


TOPO = Topology(
    hub_id="hub-00012",
    bank_id="bank-007",
    feeder_id="fdr-3",
    substation_id="sub-aen-01",
    zone="LZ_AEN",
    provider="AUSTIN_ENERGY",
)


def test_most_specific_scope_wins() -> None:
    rows: dict[tuple[str, str], list[str]] = {("FLEET", "*"): ["22:00-06:00"]}
    assert resolve(rows, TOPO) is not None and resolve(rows, TOPO).scope_kind == "FLEET"  # type: ignore[union-attr]
    for kind, ref in [
        ("PROVIDER", "AUSTIN_ENERGY"),
        ("ZONE", "LZ_AEN"),
        ("SUBSTATION", "sub-aen-01"),
        ("FEEDER", "fdr-3"),
        ("BANK", "bank-007"),
        ("HUB", "hub-00012"),
    ]:
        rows[(kind, ref)] = [f"0{len(rows)}:00-0{len(rows)}:30"]
        effective = resolve(rows, TOPO)
        assert effective is not None and (effective.scope_kind, effective.scope_ref) == (kind, ref)


def test_rows_for_other_scopes_are_ignored_and_empty_overrides_apply() -> None:
    rows = {("FLEET", "*"): ["22:00-06:00"], ("BANK", "bank-999"): ["01:00-02:00"], ("ZONE", "LZ_AEN"): []}
    effective = resolve(rows, TOPO)
    assert effective is not None and effective.windows == () and effective.scope_kind == "ZONE"
    bank_only = resolve(rows, Topology(bank_id="bank-999", zone="LZ_NORTH"))
    assert bank_only is not None and bank_only.windows == ("01:00-02:00",)
    assert resolve({}, TOPO) is None
