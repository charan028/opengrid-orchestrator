"""Tests for ogsim.customer.config: precedence (env > YAML > production refusal, mirroring
ogsim.common.config), site parsing, and Apache basic-auth credential/group resolution."""

from __future__ import annotations

import pytest

from ogsim.customer.config import (
    API_BASE_URL_ENV_VAR,
    CustomerCredentialsError,
    CustomerSiteSpec,
    default_auth_group,
    load_customer_config,
    resolve_customer_credentials,
)


def _write_yaml(tmp_path, text: str) -> str:
    path = tmp_path / "customer.yaml"
    path.write_text(text, encoding="utf-8")
    return str(path)


def test_missing_config_file_falls_back_to_defaults(tmp_path):
    config = load_customer_config(str(tmp_path / "missing.yaml"))
    assert config.sites == ()
    assert config.api_mode == "customer"


def test_loads_sites_from_yaml(tmp_path):
    path = _write_yaml(
        tmp_path,
        """
        sites:
          - customer_id: cust-1
            service_profile: DATA_CENTER
            site_id: site-1
            baseline_kw: 1000.0
        """,
    )
    config = load_customer_config(path)
    assert len(config.sites) == 1
    site = config.sites[0]
    assert site.customer_id == "cust-1"
    assert site.service_profile == "DATA_CENTER"
    assert site.baseline_kw == 1000.0
    assert site.site_id == "site-1"


def test_env_api_base_url_wins_over_yaml(tmp_path, monkeypatch: pytest.MonkeyPatch):
    path = _write_yaml(tmp_path, "api:\n  base_url: http://yaml.example:9\n")
    monkeypatch.setenv(API_BASE_URL_ENV_VAR, "https://base.example/og/api")
    config = load_customer_config(path)
    assert config.api_base_url == "https://base.example/og/api"


def test_yaml_api_base_url_used_when_env_unset(tmp_path, monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv(API_BASE_URL_ENV_VAR, raising=False)
    path = _write_yaml(tmp_path, "api:\n  base_url: http://yaml.example:9\n")
    config = load_customer_config(path)
    assert config.api_base_url == "http://yaml.example:9"


def test_api_base_url_is_empty_when_neither_env_nor_yaml_sets_it(tmp_path, monkeypatch: pytest.MonkeyPatch):
    """Lead coordination: the loopback API now 401s without the Apache proxy secret, so
    there must be no silent `http://127.0.0.1:8080` default -- an unset base URL must be
    empty, which ogsim.customer.__main__ treats as "refuse to start the API part"."""
    monkeypatch.delenv(API_BASE_URL_ENV_VAR, raising=False)
    config = load_customer_config(str(tmp_path / "missing.yaml"))
    assert config.api_base_url == ""


@pytest.mark.parametrize(
    ("service_profile", "expected_group"),
    [
        ("DATA_CENTER", "DC"),
        ("PIPELINE_AC", "PIPE"),
        ("ERCOT_ENERGY", "ERCOT"),
        ("ERCOT_AS", "ERCOT"),
        ("DIST_DEFERRAL", "DIST"),
        ("PARTNER_CAPACITY", "PARTNER"),
    ],
)
def test_default_auth_group_maps_every_service_profile(service_profile: str, expected_group: str):
    assert default_auth_group(service_profile) == expected_group


def test_unmapped_service_profile_has_no_default_auth_group():
    assert default_auth_group("SOME_UNKNOWN_PROFILE") == ""


def test_resolved_auth_group_prefers_explicit_override():
    site = CustomerSiteSpec(customer_id="c1", service_profile="DATA_CENTER", auth_group="CUSTOM")
    assert site.resolved_auth_group() == "CUSTOM"


def test_resolve_customer_credentials_reads_group_env_vars(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OGSIM_CUSTOMER_DC_USER", "og-cust-dc")
    monkeypatch.setenv("OGSIM_CUSTOMER_DC_PASSWORD", "secret")
    assert resolve_customer_credentials("dc") == ("og-cust-dc", "secret")


def test_resolve_customer_credentials_raises_when_missing(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OGSIM_CUSTOMER_PIPE_USER", raising=False)
    monkeypatch.delenv("OGSIM_CUSTOMER_PIPE_PASSWORD", raising=False)
    with pytest.raises(CustomerCredentialsError):
        resolve_customer_credentials("PIPE")


def test_resolve_customer_credentials_error_never_contains_a_password_value(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.delenv("OGSIM_CUSTOMER_DIST_USER", raising=False)
    monkeypatch.setenv("OGSIM_CUSTOMER_DIST_PASSWORD", "super-secret-value")
    with pytest.raises(CustomerCredentialsError) as excinfo:
        resolve_customer_credentials("dist")
    assert "super-secret-value" not in str(excinfo.value)
