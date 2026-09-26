"""Tests for ogsim.customer.runtime.CustomerEngine: pure per-tick decision logic, scenario/
cmd wiring, and determinism under a seed."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from ogsim.common.config import MqttSettings
from ogsim.customer.api_client import CustomerApiClient, HttpResult
from ogsim.customer.config import API_MODE_CUSTOMER, CustomerConfig, CustomerSiteSpec
from ogsim.customer.runtime import CustomerEngine, discover_contract_ids

MQTT = MqttSettings(
    host="127.0.0.1", port=1883, username="og_sim_customer", password="x", topic_root="ogtest/unit"
)

DC_SITE = CustomerSiteSpec(
    customer_id="cust-dc",
    service_profile="DATA_CENTER",
    contract_id="contract-dc",
    site_id="site-dc",
    baseline_kw=1000.0,
    request_kw=200.0,
    request_interval_s=60.0,
    meter_interval_s=2.0,
    poll_interval_s=30.0,
)
PIPE_SITE = CustomerSiteSpec(
    customer_id="cust-pipe",
    service_profile="PIPELINE_AC",
    contract_id="contract-pipe",
    corridor_id="corridor-pipe",
    line_id="line-pipe",
    limit_a=15.0,
    request_interval_s=60.0,
    meter_interval_s=2.0,
)


def _config(*sites: CustomerSiteSpec) -> CustomerConfig:
    return CustomerConfig(mqtt=MQTT, seed=7, sites=tuple(sites))


def test_tick_signals_publishes_a_data_center_site_meter_every_interval():
    engine = CustomerEngine(_config(DC_SITE))
    signals_1 = engine.tick_signals(0.0)
    assert len(signals_1) == 1
    schema_name, suffix, message = signals_1[0]
    assert schema_name == "customer_site_meter"
    assert suffix == "site/cust-dc/site-dc/meter"
    assert message["p_kw"] == pytest.approx(1000.0, abs=15.0)

    # Not due again before meter_interval_s elapses.
    assert engine.tick_signals(1.0) == []
    assert len(engine.tick_signals(2.0)) == 1


def test_tick_signals_publishes_pipeline_ac_corridor_current():
    engine = CustomerEngine(_config(PIPE_SITE))
    signals = engine.tick_signals(0.0)
    assert len(signals) == 1
    schema_name, suffix, message = signals[0]
    assert schema_name == "pipeline_corridor_current"
    assert suffix == "corridor/cust-pipe/corridor-pipe/current"
    assert message["limit_a"] == 15.0


def test_engine_is_deterministic_under_the_same_seed():
    engine_a = CustomerEngine(_config(DC_SITE), seed=42)
    engine_b = CustomerEngine(_config(DC_SITE), seed=42)
    signals_a = engine_a.tick_signals(0.0)
    signals_b = engine_b.tick_signals(0.0)
    assert signals_a == signals_b


def test_due_request_actions_respects_the_request_interval():
    engine = CustomerEngine(_config(DC_SITE))
    due_first = engine.due_request_actions(0.0)
    assert len(due_first) == 1
    state, payload = due_first[0]
    assert state.spec.customer_id == "cust-dc"
    assert payload["requested_kw"] == 200.0
    assert engine.due_request_actions(10.0) == []
    assert len(engine.due_request_actions(60.0)) == 1


def test_due_polls_respects_the_poll_interval():
    engine = CustomerEngine(_config(DC_SITE))
    assert len(engine.due_polls(0.0)) == 1
    assert engine.due_polls(10.0) == []
    assert len(engine.due_polls(30.0)) == 1


def _scenario_cmd(anomaly_type: str, target: str, params: dict, duration_s: float | None) -> dict:
    return {
        "id": "anom-1",
        "target": {"kind": "customer", "ref": target},
        "type": anomaly_type,
        "params": params,
        "start": datetime.fromtimestamp(0.0, tz=UTC).isoformat(),
        "duration_s": duration_s,
    }


def test_handle_scenario_cmd_applies_a_customer_anomaly():
    engine = CustomerEngine(_config(DC_SITE))
    cmd = _scenario_cmd("CUSTOMER_LOAD_STEP", "cust-dc", {"step_kw": 400.0}, 10.0)
    assert engine.handle_scenario_cmd(cmd) is True
    assert engine.anomalies.modifiers["cust-dc"].load_step_kw == 400.0


def test_handle_scenario_cmd_ignores_a_non_customer_anomaly_type():
    engine = CustomerEngine(_config(DC_SITE))
    cmd = _scenario_cmd("SCADA_BANK_OVERLOAD", "cust-dc", {}, 10.0)
    cmd["target"] = {"kind": "bank", "ref": "bank-000"}
    assert engine.handle_scenario_cmd(cmd) is False


def test_load_step_anomaly_raises_the_published_site_meter_reading():
    engine = CustomerEngine(_config(DC_SITE))
    engine.handle_scenario_cmd(_scenario_cmd("CUSTOMER_LOAD_STEP", "cust-dc", {"step_kw": 500.0}, 10.0))
    _, _, message = engine.tick_signals(0.0)[0]
    assert message["p_kw"] > 1400.0


def test_site_meter_stale_anomaly_suppresses_the_site_meter_publish():
    engine = CustomerEngine(_config(DC_SITE))
    engine.handle_scenario_cmd(_scenario_cmd("CUSTOMER_SITE_METER_STALE", "cust-dc", {}, 10.0))
    assert engine.tick_signals(0.0) == []


def test_request_burst_anomaly_adds_extra_opportunity_submissions():
    engine = CustomerEngine(_config(DC_SITE))
    engine.handle_scenario_cmd(_scenario_cmd("CUSTOMER_REQUEST_BURST", "cust-dc", {"count": 4}, 1.0))
    due = engine.due_request_actions(0.0)
    assert len(due) == 1 + 4


def test_malformed_request_anomaly_adds_a_malformed_payload():
    engine = CustomerEngine(_config(DC_SITE))
    engine.handle_scenario_cmd(_scenario_cmd("CUSTOMER_MALFORMED_REQUEST", "cust-dc", {}, 1.0))
    due = engine.due_request_actions(0.0)
    assert len(due) == 2
    assert due[1][1]["requested_kw"] > 1_000_000.0


def test_due_request_actions_uses_the_configured_contract_id_by_default():
    site = CustomerSiteSpec(
        customer_id="cust-dc", service_profile="DATA_CENTER", site_id="site-dc", contract_id="contract-dc"
    )
    engine = CustomerEngine(_config(site))
    _, payload = engine.due_request_actions(0.0)[0]
    assert payload["contract_id"] == "contract-dc"


# ---------------------------------------------------------------------------
# discover_contract_ids (lead coordination: read contract ids from the orchestrator's own
# obligations/contracts at startup rather than trusting a hard-coded YAML value).
# ---------------------------------------------------------------------------


class _FakeDiscoveryTransport:
    def __init__(self, contracts: list[dict], obligations: list[dict]) -> None:
        self._contracts = contracts
        self._obligations = obligations

    async def get(self, path: str, auth: tuple[str, str]) -> HttpResult:
        if path.endswith("/contracts"):
            return HttpResult(200, {"contracts": self._contracts})
        return HttpResult(200, {"obligations": self._obligations})

    async def post(self, path: str, json: dict, auth: tuple[str, str]) -> HttpResult:  # pragma: no cover
        raise AssertionError("discover_contract_ids must not POST")


async def test_discover_contract_ids_prefers_an_obligations_contract_id():
    site = CustomerSiteSpec(customer_id="cust-dc", service_profile="DATA_CENTER", site_id="site-dc")
    engine = CustomerEngine(_config(site))
    transport = _FakeDiscoveryTransport(
        contracts=[{"contract_id": "from-contracts"}], obligations=[{"contract_id": "from-obligation"}]
    )
    apis = {"DC": CustomerApiClient(transport, ("u", "p"), mode=API_MODE_CUSTOMER)}
    await discover_contract_ids(apis, engine)
    assert engine.operators["cust-dc"].contract_id == "from-obligation"


async def test_discover_contract_ids_falls_back_to_the_contracts_list():
    site = CustomerSiteSpec(customer_id="cust-dc", service_profile="DATA_CENTER", site_id="site-dc")
    engine = CustomerEngine(_config(site))
    transport = _FakeDiscoveryTransport(contracts=[{"contract_id": "from-contracts"}], obligations=[])
    apis = {"DC": CustomerApiClient(transport, ("u", "p"), mode=API_MODE_CUSTOMER)}
    await discover_contract_ids(apis, engine)
    assert engine.operators["cust-dc"].contract_id == "from-contracts"


async def test_discover_contract_ids_keeps_the_configured_value_when_nothing_is_found():
    site = CustomerSiteSpec(
        customer_id="cust-dc", service_profile="DATA_CENTER", site_id="site-dc", contract_id="configured"
    )
    engine = CustomerEngine(_config(site))
    transport = _FakeDiscoveryTransport(contracts=[], obligations=[])
    apis = {"DC": CustomerApiClient(transport, ("u", "p"), mode=API_MODE_CUSTOMER)}
    await discover_contract_ids(apis, engine)
    assert engine.operators["cust-dc"].contract_id == "configured"


async def test_discover_contract_ids_skips_an_operator_with_no_matching_api_client():
    site = CustomerSiteSpec(
        customer_id="cust-dc", service_profile="DATA_CENTER", site_id="site-dc", contract_id="configured"
    )
    engine = CustomerEngine(_config(site))
    await discover_contract_ids({}, engine)
    assert engine.operators["cust-dc"].contract_id == "configured"
