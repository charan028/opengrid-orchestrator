"""The three additional-service operators (14-additional-services.md) and the shipped customer.yaml's
ids: disabled operators stay out of the process, auth groups map to og-cust-pjm/-mobile/-largeld,
MOBILE_STORAGE/LARGE_LOAD publish a site meter while PJM_CAPACITY does not, and every customer id
matches its seed (0002_seed_demo.sql for c02/c04/c05, the dev seeds for c6..ca)."""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from ogsim.common.config import MqttSettings
from ogsim.customer.config import (
    DEFAULT_CUSTOMER_CONFIG_PATH,
    CustomerConfig,
    CustomerSiteSpec,
    default_auth_group,
    load_customer_config,
)
from ogsim.customer.runtime import CustomerEngine

REPO = Path(__file__).resolve().parents[2]
SEED_FILES = (
    REPO / "orchestrator" / "migrations" / "0002_seed_demo.sql",
    REPO / "dev" / "seed" / "customer_services_seed.sql",
    REPO / "dev" / "seed" / "services_seed.sql",
)
MQTT = MqttSettings(
    host="127.0.0.1", port=1883, username="og_sim_customer", password="x", topic_root="ogtest/u"
)


@pytest.mark.parametrize(
    ("profile", "group"), [("PJM_CAPACITY", "PJM"), ("MOBILE_STORAGE", "MOBILE"), ("LARGE_LOAD", "LARGELD")]
)
def test_additional_services_map_to_their_apache_accounts(profile: str, group: str) -> None:
    assert default_auth_group(profile) == group


def test_disabled_operators_are_not_loaded(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OGSIM_CUSTOMER_CONFIG", raising=False)
    profiles = {s.service_profile for s in load_customer_config(str(DEFAULT_CUSTOMER_CONFIG_PATH)).sites}
    assert profiles == {"DATA_CENTER", "PIPELINE_AC", "ERCOT_ENERGY", "DIST_DEFERRAL", "PARTNER_CAPACITY"}


def test_enabled_flag_is_honoured(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OGSIM_CUSTOMER_CONFIG", raising=False)
    cfg = tmp_path / "c.yaml"
    cfg.write_text(
        "sites:\n"
        "  - {customer_id: a, service_profile: LARGE_LOAD, enabled: true}\n"
        "  - {customer_id: b, service_profile: PJM_CAPACITY, enabled: false}\n",
        encoding="utf-8",
    )
    assert [s.customer_id for s in load_customer_config(str(cfg)).sites] == ["a"]


def test_every_configured_customer_id_is_seeded() -> None:
    seeded = set()
    for path in SEED_FILES:
        seeded |= set(re.findall(r"00000000-0000-7000-8000-[0-9a-f]{12}", path.read_text(encoding="utf-8")))
    ids = re.findall(r"customer_id:\s*(\S+)", DEFAULT_CUSTOMER_CONFIG_PATH.read_text(encoding="utf-8"))
    assert len(ids) == 8
    assert set(ids) <= seeded


@pytest.mark.parametrize(
    ("profile", "publishes"), [("MOBILE_STORAGE", True), ("LARGE_LOAD", True), ("PJM_CAPACITY", False)]
)
def test_site_meter_only_for_site_metered_profiles(profile: str, publishes: bool) -> None:
    spec = CustomerSiteSpec(customer_id="c", service_profile=profile, site_id="s", baseline_kw=500.0)
    signals = CustomerEngine(CustomerConfig(mqtt=MQTT, seed=1, sites=(spec,))).tick_signals(0.0)
    assert [(name, suffix) for name, suffix, _ in signals] == (
        [("customer_site_meter", "site/c/s/meter")] if publishes else []
    )
