"""Tests for ogsim.customer.__main__._build_api_clients: refuses to build any API client
(returning {}, which ogsim.customer.runtime.run_customer treats as "MQTT-only") when
OGSIM_CUSTOMER_API_BASE is unset -- the orchestrator's loopback API now 401s without the
Apache proxy secret, so guessing a default is worse than not calling it."""

from __future__ import annotations

import pytest

from ogsim.customer.__main__ import _build_api_clients
from ogsim.customer.api_client import CustomerApiClient
from ogsim.customer.config import API_MODE_CUSTOMER, CustomerConfig, CustomerSiteSpec

DC_SITE = CustomerSiteSpec(customer_id="cust-dc", service_profile="DATA_CENTER", site_id="site-dc")
PIPE_SITE = CustomerSiteSpec(
    customer_id="cust-pipe", service_profile="PIPELINE_AC", corridor_id="corridor-pipe"
)


def test_build_api_clients_refuses_when_base_url_is_unset():
    config = CustomerConfig(mqtt=None, api_base_url="", sites=(DC_SITE,))  # type: ignore[arg-type]
    clients = _build_api_clients(config, timeout_s=10.0)
    assert clients == {}


def test_build_api_clients_builds_one_client_per_auth_group(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setenv("OGSIM_CUSTOMER_DC_USER", "og-cust-dc")
    monkeypatch.setenv("OGSIM_CUSTOMER_DC_PASSWORD", "secret-dc")
    monkeypatch.setenv("OGSIM_CUSTOMER_PIPE_USER", "og-cust-pipe")
    monkeypatch.setenv("OGSIM_CUSTOMER_PIPE_PASSWORD", "secret-pipe")
    config = CustomerConfig(
        mqtt=None,  # type: ignore[arg-type]
        api_base_url="https://base.example/og/api",
        api_mode=API_MODE_CUSTOMER,
        sites=(DC_SITE, PIPE_SITE),
    )
    clients = _build_api_clients(config, timeout_s=10.0)
    assert set(clients) == {"DC", "PIPE"}
    assert all(isinstance(c, CustomerApiClient) for c in clients.values())
