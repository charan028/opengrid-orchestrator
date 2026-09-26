"""Schema validation tests for every message ogsim.customer publishes: the DATA_CENTER
site meter and PIPELINE_AC corridor current (interfaces/mqtt/customer_site_meter.schema.json,
pipeline_corridor_current.schema.json)."""

from __future__ import annotations

import numpy as np
import pytest

from ogsim.common.schemas import SchemaValidationError, validate
from ogsim.customer.config import CustomerSiteSpec
from ogsim.customer.signals import (
    build_corridor_current_message,
    build_site_meter_message,
    corridor_current_topic,
    site_meter_topic,
)

DC_SITE = CustomerSiteSpec(
    customer_id="cust-dc-1", service_profile="DATA_CENTER", site_id="site-1", baseline_kw=2000.0
)
PIPE_SITE = CustomerSiteSpec(
    customer_id="cust-pipe-1",
    service_profile="PIPELINE_AC",
    corridor_id="corridor-1",
    line_id="line-1",
    limit_a=15.0,
)


@pytest.fixture
def rng() -> np.random.Generator:
    return np.random.default_rng(1)


def test_site_meter_message_validates_against_schema(rng):
    message = build_site_meter_message(DC_SITE, now=0.0, p_kw=1800.0, rng=rng)
    validate("customer_site_meter", message)


def test_site_meter_message_has_expected_identity_fields(rng):
    message = build_site_meter_message(DC_SITE, now=0.0, p_kw=1800.0, rng=rng)
    assert message["site_id"] == "site-1"
    assert message["customer_id"] == "cust-dc-1"
    assert message["p_kw"] == pytest.approx(1800.0, abs=5.0)
    assert message["quality"] == "GOOD"


def test_site_meter_message_rejects_when_a_required_field_is_missing(rng):
    message = build_site_meter_message(DC_SITE, now=0.0, p_kw=1800.0, rng=rng)
    del message["freq_hz"]
    with pytest.raises(SchemaValidationError):
        validate("customer_site_meter", message)


def test_site_meter_topic_matches_the_published_topic_shape():
    assert site_meter_topic(DC_SITE) == "site/cust-dc-1/site-1/meter"


def test_corridor_current_message_validates_against_schema(rng):
    message = build_corridor_current_message(PIPE_SITE, now=0.0, i_ac_a=5.0, rng=rng)
    validate("pipeline_corridor_current", message)


def test_corridor_current_message_has_expected_identity_fields(rng):
    message = build_corridor_current_message(PIPE_SITE, now=0.0, i_ac_a=5.0, rng=rng)
    assert message["corridor_id"] == "corridor-1"
    assert message["line_id"] == "line-1"
    assert message["customer_id"] == "cust-pipe-1"
    assert message["limit_a"] == 15.0


def test_corridor_current_message_current_never_negative(rng):
    message = build_corridor_current_message(PIPE_SITE, now=0.0, i_ac_a=0.0, rng=rng)
    assert message["i_ac_a"] >= 0.0


def test_corridor_current_topic_matches_the_published_topic_shape():
    assert corridor_current_topic(PIPE_SITE) == "corridor/cust-pipe-1/corridor-1/current"
