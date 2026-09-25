"""Tests for loading and validating config/random.yaml."""

from __future__ import annotations

from pathlib import Path

import pytest

from ogsim.control import catalogue
from ogsim.control.random_config import RandomConfigError, load_random_config

CONFIG_PATH = Path(__file__).resolve().parents[1] / "config" / "random.yaml"


def test_shipped_random_config_loads_without_error():
    cfg = load_random_config(CONFIG_PATH)
    assert cfg.seed == 20260926
    assert cfg.profile == "normal"
    assert not cfg.paused
    assert cfg.max_concurrent > 0


def test_shipped_random_config_only_references_known_catalogue_types():
    cfg = load_random_config(CONFIG_PATH)
    for type_id in cfg.types:
        assert catalogue.owner_of(type_id) is not None


def test_shipped_random_config_covers_every_catalogue_type():
    cfg = load_random_config(CONFIG_PATH)
    configured = set(cfg.types)
    all_types = {a.id for a in catalogue.CATALOGUE}
    assert configured == all_types


def test_missing_config_file_returns_safe_defaults(tmp_path: Path):
    cfg = load_random_config(tmp_path / "missing.yaml")
    assert cfg.types == {}
    assert cfg.profile == "normal"


def test_unknown_anomaly_type_in_config_raises(tmp_path: Path):
    bad = tmp_path / "bad.yaml"
    bad.write_text("types:\n  not_a_real_type:\n    rate_per_hour: 1.0\n", encoding="utf-8")
    with pytest.raises(RandomConfigError):
        load_random_config(bad)


def test_profiles_multiplier_lookup_falls_back_to_identity_for_unknown_profile():
    cfg = load_random_config(CONFIG_PATH)
    cfg.profile = "not-configured"
    multipliers = cfg.active_multipliers()
    assert multipliers.rate_multiplier == 1.0
    assert multipliers.duration_multiplier == 1.0
