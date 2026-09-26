"""Site-meter and corridor-current ingest: schema conformance, topic scoping, freshness, feedback-ref
resolution, and batched persistence with no per-message writes."""

from __future__ import annotations

import json
from collections.abc import Sequence
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import jsonschema
import pytest

import opengrid.site_ingest as site_ingest
from opengrid.site_ingest import (
    CorridorCurrentReading,
    FeedbackRefError,
    SiteIngestRejectedError,
    SiteMeterReading,
    parse_feedback_ref,
)

ROOT = "ogtest/ws"
NOW = datetime(2026, 9, 26, 12, 0, 0, tzinfo=UTC)
SCHEMAS = Path(__file__).resolve().parents[4] / "interfaces" / "mqtt"
SITE_TOPIC = f"{ROOT}/site/cust-a/site-1/meter"
CORRIDOR_TOPIC = f"{ROOT}/corridor/cust-p/corr-1/current"


def site_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "site_id": "site-1",
        "customer_id": "cust-a",
        "ts": NOW.isoformat(),
        "p_kw": 850.0,
        "q_kvar": 120.0,
        "v_rms_a_v": 277.0,
        "v_rms_b_v": 276.5,
        "v_rms_c_v": 277.4,
        "i_rms_a_a": 1000.0,
        "i_rms_b_a": 1010.0,
        "i_rms_c_a": 990.0,
        "freq_hz": 60.01,
        "pf": 0.97,
        "thd_v_pct": 1.2,
        "thd_i_pct": 2.1,
        "quality": "GOOD",
    }
    return payload | overrides


def corridor_payload(**overrides: Any) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "corridor_id": "corr-1",
        "line_id": "line-138-7",
        "customer_id": "cust-p",
        "ts": NOW.isoformat(),
        "i_ac_a": 11.5,
        "limit_a": 15.0,
        "quality": "GOOD",
    }
    return payload | overrides


class FakeBackend:
    def __init__(self) -> None:
        self.batches: list[tuple[list[SiteMeterReading], list[CorridorCurrentReading]]] = []
        self.fail = False

    async def insert_batch(
        self, site_rows: Sequence[SiteMeterReading], corridor_rows: Sequence[CorridorCurrentReading]
    ) -> None:
        if self.fail:
            raise OSError("checkpoint stall")
        self.batches.append((list(site_rows), list(corridor_rows)))


@pytest.fixture
def backend() -> FakeBackend:
    fake = FakeBackend()
    site_ingest.configure(fake, buffer_max=5)
    yield fake
    site_ingest.reset_for_testing()


def _schema_accepts(filename: str, payload: dict[str, Any]) -> bool:
    schema = json.loads((SCHEMAS / filename).read_text(encoding="utf-8"))
    return not list(jsonschema.Draft202012Validator(schema).iter_errors(payload))


INVALID_SITE = [
    {"p_kw": "850"},
    {"pf": 1.2},
    {"v_rms_a_v": -1.0},
    {"quality": "OK"},
    {"extra": 1},
    {"thd_i_pct": True},
    {"site_id": ""},
]


@pytest.mark.parametrize("override", INVALID_SITE)
def test_site_model_rejects_what_the_schema_rejects(override: dict[str, Any]) -> None:
    payload = site_payload(**override)
    assert not _schema_accepts("customer_site_meter.schema.json", payload)
    with pytest.raises(ValueError):
        SiteMeterReading.model_validate(payload)


def test_site_and_corridor_models_accept_valid_schema_payloads() -> None:
    assert _schema_accepts("customer_site_meter.schema.json", site_payload())
    assert _schema_accepts("pipeline_corridor_current.schema.json", corridor_payload())
    SiteMeterReading.model_validate(site_payload())
    CorridorCurrentReading.model_validate(corridor_payload())


def test_models_carry_exactly_the_schema_fields() -> None:
    for model, filename in (
        (SiteMeterReading, "customer_site_meter.schema.json"),
        (CorridorCurrentReading, "pipeline_corridor_current.schema.json"),
    ):
        schema = json.loads((SCHEMAS / filename).read_text(encoding="utf-8"))
        assert set(model.model_fields) == set(schema["properties"]) == set(schema["required"])


@pytest.mark.parametrize(
    "override", [{"i_ac_a": -0.1}, {"quality": "good"}, {"line_id": ""}, {"limit_a": "15"}]
)
def test_corridor_model_rejects_what_the_schema_rejects(override: dict[str, Any]) -> None:
    payload = corridor_payload(**override)
    assert not _schema_accepts("pipeline_corridor_current.schema.json", payload)
    with pytest.raises(ValueError):
        CorridorCurrentReading.model_validate(payload)


def test_invalid_payload_is_rejected_and_nothing_is_held(backend) -> None:
    with pytest.raises(SiteIngestRejectedError):
        site_ingest.ingest_site_meter(site_payload(pf=2.0), topic=SITE_TOPIC, root=ROOT, now=NOW)
    assert site_ingest.pending_count() == 0
    assert site_ingest.latest_site_meter("cust-a", "site-1") is None
    assert site_ingest.counters()["rejected_invalid"] == 1


@pytest.mark.parametrize(
    "topic",
    [
        f"{ROOT}/site/cust-b/site-1/meter",  # another customer's topic
        f"{ROOT}/site/cust-a/site-2/meter",
        f"{ROOT}/site/cust-a/site-1/other",
        "og/v1/site/cust-a/site-1/meter",  # another root
    ],
)
def test_payload_must_match_its_topic(backend, topic: str) -> None:
    with pytest.raises(SiteIngestRejectedError):
        site_ingest.ingest_site_meter(site_payload(), topic=topic, root=ROOT, now=NOW)
    assert site_ingest.pending_count() == 0


def test_future_timestamp_is_rejected(backend) -> None:
    ahead = (NOW + timedelta(seconds=30)).isoformat()
    with pytest.raises(SiteIngestRejectedError):
        site_ingest.ingest_site_meter(site_payload(ts=ahead), topic=SITE_TOPIC, root=ROOT, now=NOW)


def test_older_reading_never_replaces_the_latest(backend) -> None:
    assert site_ingest.ingest_site_meter(site_payload(p_kw=900.0), topic=SITE_TOPIC, root=ROOT, now=NOW)
    older = (NOW - timedelta(seconds=4)).isoformat()
    assert not site_ingest.ingest_site_meter(
        site_payload(ts=older, p_kw=1.0), topic=SITE_TOPIC, root=ROOT, now=NOW
    )
    latest = site_ingest.latest_site_meter("cust-a", "site-1")
    assert latest is not None and latest.p_kw == 900.0
    assert site_ingest.pending_count() == 2  # both still persisted for history


def test_resolve_feedback_signal_reports_value_and_freshness(backend) -> None:
    site_ingest.ingest_site_meter(site_payload(p_kw=812.5), topic=SITE_TOPIC, root=ROOT, now=NOW)
    fresh = site_ingest.resolve_feedback_signal(
        "site_meter:site-1:p_kw", customer_id="cust-a", max_age_s=2.0, now=NOW + timedelta(seconds=1.5)
    )
    assert fresh is not None and fresh.value == 812.5 and fresh.usable
    stale = site_ingest.resolve_feedback_signal(
        "site_meter:site-1:p_kw", customer_id="cust-a", max_age_s=2.0, now=NOW + timedelta(seconds=2.5)
    )
    assert stale is not None and not stale.fresh and not stale.usable


def test_resolution_is_scoped_to_the_obligations_customer(backend) -> None:
    site_ingest.ingest_site_meter(site_payload(), topic=SITE_TOPIC, root=ROOT, now=NOW)
    assert (
        site_ingest.resolve_feedback_signal(
            "site_meter:site-1:p_kw", customer_id="cust-b", max_age_s=2, now=NOW
        )
        is None
    )


def test_non_good_quality_is_not_usable_even_when_fresh(backend) -> None:
    site_ingest.ingest_corridor_current(
        corridor_payload(quality="SUSPECT"), topic=CORRIDOR_TOPIC, root=ROOT, now=NOW
    )
    value = site_ingest.resolve_feedback_signal(
        "corridor:corr-1:i_ac_a", customer_id="cust-p", max_age_s=4, now=NOW
    )
    assert value is not None and value.fresh and not value.usable


@pytest.mark.parametrize(
    "ref",
    ["site_meter:site-1", "site_meter:site-1:bogus", "corridor:c:p_kw", "meter:s:p_kw", "site_meter::p_kw"],
)
def test_malformed_feedback_ref_is_an_error(ref: str) -> None:
    with pytest.raises(FeedbackRefError):
        parse_feedback_ref(ref)


def test_data_center_profile_template_ref_parses() -> None:
    parsed = parse_feedback_ref("site_meter:{site_id}:p_kw".format(site_id="site-dc-01"))
    assert (parsed.kind, parsed.source_id, parsed.field) == ("site_meter", "site-dc-01", "p_kw")


async def test_ingest_never_writes_and_flush_writes_one_batch(backend) -> None:
    for seconds in range(3):
        ts = (NOW + timedelta(seconds=2 * seconds)).isoformat()
        site_ingest.ingest_site_meter(
            site_payload(ts=ts), topic=SITE_TOPIC, root=ROOT, now=NOW + timedelta(seconds=6)
        )
    site_ingest.ingest_corridor_current(corridor_payload(), topic=CORRIDOR_TOPIC, root=ROOT, now=NOW)
    assert backend.batches == []
    assert await site_ingest.flush_readings() == 4
    assert len(backend.batches) == 1
    assert (len(backend.batches[0][0]), len(backend.batches[0][1])) == (3, 1)
    assert site_ingest.pending_count() == 0


async def test_failed_flush_keeps_rows_for_the_next_one(backend) -> None:
    site_ingest.ingest_site_meter(site_payload(), topic=SITE_TOPIC, root=ROOT, now=NOW)
    backend.fail = True
    with pytest.raises(OSError):
        await site_ingest.flush_readings()
    assert site_ingest.pending_count() == 1
    backend.fail = False
    assert await site_ingest.flush_readings() == 1


def test_buffer_is_bounded_dropping_the_oldest(backend) -> None:
    for seconds in range(7):
        ts = (NOW + timedelta(seconds=seconds)).isoformat()
        site_ingest.ingest_site_meter(
            site_payload(ts=ts), topic=SITE_TOPIC, root=ROOT, now=NOW + timedelta(seconds=7)
        )
    assert site_ingest.pending_count() == 5
    assert site_ingest.counters()["dropped_buffer_full"] == 2
