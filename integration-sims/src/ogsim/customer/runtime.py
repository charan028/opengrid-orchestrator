"""ogsim.customer.runtime -- CustomerEngine: per-tick decision logic for autonomous
customer operators, plus the thin async glue tying MQTT publish and the orchestrator's
customer API together.

`CustomerEngine` is pure (no I/O) and unit-testable with a `FakeClock`; `run_customer` is
the async shell that feeds it from `ogsim.common.mqtt_client` and
`ogsim.customer.api_client`. One `CustomerApiClient` per Apache basic-auth group (DC/PIPE/
ERCOT/DIST/PARTNER) is supplied by the caller, since each group's credentials differ
(`ogsim.customer.config.resolve_customer_credentials`); `run_customer` looks each operator's
client up by `CustomerSiteSpec.resolved_auth_group()`.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

import numpy as np

from ogsim.common.clock import Clock
from ogsim.common.mqtt_client import SimMqttClient
from ogsim.common.scenario import parse_scenario_cmd, utc_timestamp
from ogsim.customer.anomalies import CUSTOMER_ANOMALY_TYPES, CustomerAnomalyManager
from ogsim.customer.api_client import (
    AdmissionRejectedError,
    CustomerApiClient,
    OperatorFallbackUnsupportedError,
)
from ogsim.customer.config import SITE_METER_PROFILES, CustomerConfig, CustomerSiteSpec
from ogsim.customer.requests import build_malformed_opportunity_payload, build_opportunity_payload
from ogsim.customer.signals import (
    build_corridor_current_message,
    build_site_meter_message,
    corridor_current_topic,
    site_meter_topic,
)

logger = logging.getLogger(__name__)

WINDOW_LOOKAHEAD_S = 900.0  # opportunity requests target a window starting 15 min out
WINDOW_DURATION_S = 3600.0
DEFAULT_TICK_INTERVAL_S = 2.0


@dataclass
class OperatorState:
    spec: CustomerSiteSpec
    next_meter_at: float = 0.0
    next_request_at: float = 0.0
    next_poll_at: float = 0.0
    known_invoice_ids: set[str] = field(default_factory=set)
    #: Discovered at startup from the orchestrator's own obligations/contracts (lead
    #: coordination: read contract ids from the API rather than hard-coding them where
    #: possible); "" until `discover_contract_ids` runs or finds nothing, in which case
    #: `contract_id` falls back to `spec.contract_id` (e.g. a value hand-set from
    #: dev/seed/customer_services_seed.sql).
    discovered_contract_id: str = ""

    @property
    def contract_id(self) -> str:
        return self.discovered_contract_id or self.spec.contract_id


class CustomerEngine:
    """Pure per-tick logic for every configured customer operator: builds outbound MQTT
    site-measurement messages and decides which API actions are due this tick. I/O (MQTT
    publish, HTTP calls) is performed by `run_customer`/the caller, not here, so this class
    is directly unit-testable and deterministic under a seed (BUILD.md design constraint)."""

    def __init__(self, config: CustomerConfig, seed: int | None = None) -> None:
        self.config = config
        self.rng = np.random.default_rng(seed if seed is not None else config.seed)
        self.operators: dict[str, OperatorState] = {s.customer_id: OperatorState(s) for s in config.sites}
        self.anomalies = CustomerAnomalyManager(list(self.operators))

    def handle_scenario_cmd(self, raw: dict[str, Any]) -> bool:
        cmd = parse_scenario_cmd(raw)
        if cmd.catalogue_type not in CUSTOMER_ANOMALY_TYPES:
            return False
        self.anomalies.start(
            cmd.id, cmd.catalogue_type, cmd.target_ref, cmd.params, cmd.start_epoch, cmd.duration_s
        )
        return True

    def tick_signals(self, now: float) -> list[tuple[str, str, dict[str, Any]]]:
        """Returns `(schema_name, topic_suffix, message)` triples due to publish this tick
        (DATA_CENTER site meter / PIPELINE_AC corridor current, at each operator's own
        `meter_interval_s`)."""
        self.anomalies.tick(now)
        out: list[tuple[str, str, dict[str, Any]]] = []
        for state in self.operators.values():
            spec = state.spec
            if now < state.next_meter_at:
                continue
            m = self.anomalies.modifiers[spec.customer_id]
            if spec.service_profile in SITE_METER_PROFILES and spec.site_id:
                state.next_meter_at = now + spec.meter_interval_s
                if not m.meter_stale:
                    noise = float(self.rng.normal(0.0, spec.baseline_kw * 0.01 + 0.01))
                    p_kw = spec.baseline_kw + m.load_step_kw + noise
                    message = build_site_meter_message(spec, now, p_kw, self.rng)
                    out.append(("customer_site_meter", site_meter_topic(spec), message))
            elif spec.service_profile == "PIPELINE_AC" and spec.corridor_id:
                state.next_meter_at = now + spec.meter_interval_s
                i_ac = max(0.0, spec.limit_a * 0.3 + m.current_surge_a)
                message = build_corridor_current_message(spec, now, i_ac, self.rng)
                out.append(("pipeline_corridor_current", corridor_current_topic(spec), message))
        return out

    def due_request_actions(self, now: float) -> list[tuple[OperatorState, dict[str, Any]]]:
        """Returns `(operator_state, opportunity_payload)` pairs due to submit this tick,
        including any `request_burst`-inflated extra payloads and a malformed/oversize
        payload when `malformed_request` is pending (must be rejected by R-ADMIT-REJECT)."""
        due: list[tuple[OperatorState, dict[str, Any]]] = []
        for state in self.operators.values():
            spec = state.spec
            if now < state.next_request_at:
                continue
            state.next_request_at = now + spec.request_interval_s
            window_start = utc_timestamp(now + WINDOW_LOOKAHEAD_S)
            window_end = utc_timestamp(now + WINDOW_LOOKAHEAD_S + WINDOW_DURATION_S)
            contract_id = state.contract_id
            payload = build_opportunity_payload(spec, window_start, window_end, contract_id=contract_id)
            due.append((state, payload))
            burst = self.anomalies.take_pending_request_burst(spec.customer_id)
            due.extend((state, payload) for _ in range(burst))
            if self.anomalies.take_pending_malformed_request(spec.customer_id):
                malformed = build_malformed_opportunity_payload(spec, contract_id=contract_id)
                due.append((state, malformed))
        return due

    def due_polls(self, now: float) -> list[OperatorState]:
        """Returns operators due to poll obligation/invoice state this tick."""
        due = []
        for state in self.operators.values():
            if now >= state.next_poll_at:
                state.next_poll_at = now + state.spec.poll_interval_s
                due.append(state)
        return due


async def _publish_signals(client: SimMqttClient, signals: list[tuple[str, str, dict[str, Any]]]) -> None:
    by_schema: dict[str, list[tuple[str, dict[str, Any]]]] = {}
    for schema_name, suffix, message in signals:
        by_schema.setdefault(schema_name, []).append((suffix, message))
    for schema_name, items in by_schema.items():
        await client.publish_batch(schema_name, items, qos=0)


def _client_for(apis: dict[str, CustomerApiClient], state: OperatorState) -> CustomerApiClient | None:
    group = state.spec.resolved_auth_group()
    client = apis.get(group)
    if client is None:
        logger.warning(
            "no API client configured for auth group %r (customer %s)", group, state.spec.customer_id
        )
    return client


async def _submit_opportunities(
    apis: dict[str, CustomerApiClient], due: list[tuple[OperatorState, dict[str, Any]]]
) -> None:
    for state, payload in due:
        client = _client_for(apis, state)
        if client is None:
            continue
        try:
            await client.submit_opportunity(payload)
        except AdmissionRejectedError as exc:
            logger.info("opportunity rejected for %s: %s", state.spec.customer_id, exc.reason_code)


async def _run_polls(
    apis: dict[str, CustomerApiClient], engine: CustomerEngine, due: list[OperatorState]
) -> None:
    for state in due:
        client = _client_for(apis, state)
        if client is None:
            continue
        customer_id = state.spec.customer_id
        try:
            obligations = await client.get_obligations()
            invoices = await client.get_invoices()
        except OperatorFallbackUnsupportedError:
            continue
        state.known_invoice_ids.update(str(inv.get("id", "")) for inv in invoices if inv.get("id"))
        if engine.anomalies.take_pending_late_cancellation(customer_id) and obligations:
            await client.cancel_obligation(str(obligations[0].get("id", "")))
        reason = engine.anomalies.take_pending_invoice_dispute(customer_id)
        if reason is not None and invoices:
            await client.dispute_invoice(str(invoices[0].get("id", "")), reason)


def _first_contract_id(contracts: list[dict[str, Any]], obligations: list[dict[str, Any]]) -> str:
    """Picks a contract id from the orchestrator's own state: prefer an obligation's own
    `contract_id` (it names the contract actually under delivery), else the first contract
    the customer's `get_contracts` call returns."""
    for obligation in obligations:
        contract_id = obligation.get("contract_id")
        if contract_id:
            return str(contract_id)
    for contract in contracts:
        contract_id = contract.get("contract_id") or contract.get("id")
        if contract_id:
            return str(contract_id)
    return ""


async def discover_contract_ids(apis: dict[str, CustomerApiClient], engine: CustomerEngine) -> None:
    """Startup bootstrap (lead coordination): reads each operator's contract id from the
    orchestrator's own obligations/contracts rather than trusting a hard-coded YAML value,
    when the API can supply it. An operator whose `CustomerSiteSpec.contract_id` was already
    set (e.g. from dev/seed/customer_services_seed.sql) keeps that as a fallback if
    discovery finds nothing or the API isn't reachable yet."""
    for state in engine.operators.values():
        client = _client_for(apis, state)
        if client is None:
            continue
        try:
            contracts = await client.get_contracts()
            obligations = await client.get_obligations()
        except OperatorFallbackUnsupportedError:
            continue
        contract_id = _first_contract_id(contracts, obligations)
        if contract_id:
            state.discovered_contract_id = contract_id
        else:
            logger.info(
                "no contract discovered via the API for customer %s; keeping configured contract_id",
                state.spec.customer_id,
            )


async def run_customer(
    client: SimMqttClient, apis: dict[str, CustomerApiClient], engine: CustomerEngine, clock: Clock
) -> None:
    """Async shell: ticks `engine` on the fastest configured meter interval, publishing due
    signals over MQTT and issuing due API actions against each operator's own auth-group
    client. If `apis` is empty (lead coordination: `OGSIM_CUSTOMER_API_BASE` unset), the API
    part is skipped entirely and only the MQTT site-measurement signals run.

    One tick's body is wrapped in its own try/except (mirrors `ogsim.fleet.runtime.run_fleet`):
    a transient publish/API failure must be logged and the loop must keep ticking on
    schedule, never silently end telemetry for the rest of the process's life."""
    tick_interval_s = min((s.meter_interval_s for s in engine.config.sites), default=DEFAULT_TICK_INTERVAL_S)
    while True:
        now = clock.now()
        try:
            await _publish_signals(client, engine.tick_signals(now))
            if apis:
                await _submit_opportunities(apis, engine.due_request_actions(now))
                await _run_polls(apis, engine, engine.due_polls(now))
        except Exception:
            logger.exception("customer tick failed; continuing loop")
        await clock.sleep(tick_interval_s)


__all__ = ["CustomerEngine", "OperatorState", "discover_contract_ids", "run_customer"]
