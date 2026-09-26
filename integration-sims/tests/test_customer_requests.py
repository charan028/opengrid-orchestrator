"""Tests for ogsim.customer.requests: opportunity payload builders for every service
profile, and the malformed/oversize payload for R-ADMIT-REJECT."""

from __future__ import annotations

from ogsim.customer.config import CustomerSiteSpec
from ogsim.customer.requests import (
    OVERSIZE_REQUESTED_KW,
    build_malformed_opportunity_payload,
    build_opportunity_payload,
)

SITE = CustomerSiteSpec(
    customer_id="cust-1", service_profile="DATA_CENTER", contract_id="contract-1", request_kw=250.0
)


def test_build_opportunity_payload_uses_the_site_default_request_kw():
    payload = build_opportunity_payload(SITE, "2026-09-26T00:00:00Z", "2026-09-26T01:00:00Z")
    assert payload["requested_kw"] == 250.0
    assert payload["contract_id"] == "contract-1"
    assert payload["customer_id"] == "cust-1"
    assert payload["service_profile"] == "DATA_CENTER"
    assert payload["window_start"] == "2026-09-26T00:00:00Z"
    assert payload["window_end"] == "2026-09-26T01:00:00Z"


def test_build_opportunity_payload_honors_an_explicit_requested_kw():
    payload = build_opportunity_payload(SITE, "s", "e", requested_kw=999.0)
    assert payload["requested_kw"] == 999.0


def test_malformed_opportunity_payload_is_grossly_oversize():
    payload = build_malformed_opportunity_payload(SITE)
    assert payload["requested_kw"] == OVERSIZE_REQUESTED_KW
    assert payload["requested_kw"] > 1_000_000.0


def test_malformed_opportunity_payload_has_an_invalid_timestamp_shape():
    payload = build_malformed_opportunity_payload(SITE)
    assert payload["window_start"] == "not-a-timestamp"
    assert payload["window_end"] == "not-a-timestamp"


def test_malformed_opportunity_payload_carries_an_unexpected_field():
    payload = build_malformed_opportunity_payload(SITE)
    assert "unexpected_field" in payload
    assert len(payload["unexpected_field"]) > 1000
