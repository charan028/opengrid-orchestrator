"""Interface layer: config selection (default MQTT, nothing changes), sinks, instruction tracking and the
shared point assembler."""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any
from uuid import uuid4

import pytest

from opengrid.core.models.mqtt import ScadaBankSignal, ScadaUtilityInstruction
from opengrid.integrations.bank_points import BankPointAssembler, MappedPoint
from opengrid.integrations.config import (
    build_market_submission,
    build_scada_source,
    load_integrations_settings,
)
from opengrid.integrations.instructions import DesiredInstruction, InstructionTracker
from opengrid.integrations.interfaces import MarketSubmission, ScadaSource
from opengrid.integrations.mqtt_backend import MqttScadaSource
from opengrid.integrations.sinks import CollectingSink, FleetScadaSink, MqttBridgeSink
from opengrid.integrations.tls import TlsSettings, build_client_ssl_context
from opengrid.platform.mqtt import SchemaValidationError

NOW = datetime(2026, 9, 26, 18, 0, tzinfo=UTC)


def test_no_integrations_table_means_mqtt_and_no_market() -> None:
    settings = load_integrations_settings(None)
    source = build_scada_source(settings.scada)
    assert isinstance(source, MqttScadaSource) and source.backend == "mqtt"
    assert isinstance(source, ScadaSource)
    assert build_market_submission(settings.market) is None


def test_backend_selection_by_config() -> None:
    raw = {
        "scada": {
            "backend": "dnp3",
            "dnp3": {"outstations": [{"name": "sub-1", "host": "10.0.0.5", "banks": ["bank-000"]}]},
        },
        "market": {
            "backend": "ercot_mms",
            "ercot_mms": {
                "endpoint": "http://127.0.0.1:18090/ews/",
                "qse_code": "Q",
                "user_id": "u",
                "signing": "none",
            },
        },
    }
    settings = load_integrations_settings(raw)
    assert build_scada_source(settings.scada).backend == "dnp3"
    # a configured market backend stays OFF until explicitly enabled
    assert build_market_submission(settings.market) is None
    enabled = load_integrations_settings({**raw, "market": {**raw["market"], "enabled": True}})
    market = build_market_submission(enabled.market)
    assert market is not None and isinstance(market, MarketSubmission) and market.backend == "ercot_mms"


@pytest.mark.parametrize("backend", ["dnp3", "iccp", "ieee2030_5"])
def test_protocol_backend_without_its_table_is_a_config_error(backend: str) -> None:
    with pytest.raises(ValueError, match=f"integrations.scada.{backend}"):
        load_integrations_settings({"scada": {"backend": backend}})


def test_iccp_and_2030_5_factories() -> None:
    iccp = load_integrations_settings(
        {
            "scada": {
                "backend": "iccp",
                "iccp": {
                    "host": "127.0.0.1",
                    "bilateral_table": {
                        "bilateral_table_id": "BLT-1",
                        "local_domain": "OG",
                        "remote_domain": "UT",
                        "data_values": [
                            {
                                "name": "B0_KVA",
                                "bank_id": "bank-000",
                                "role": "APPARENT_POWER_KVA",
                                "scale": 1000,
                            }
                        ],
                    },
                },
            }
        }
    )
    assert build_scada_source(iccp.scada).backend == "iccp"
    sep2 = load_integrations_settings(
        {
            "scada": {
                "backend": "ieee2030_5",
                "ieee2030_5": {
                    "base_url": "https://127.0.0.1:18443",
                    "banks": [{"bank_id": "bank-000", "sfdi": 123, "rated_kw": 600}],
                },
            }
        }
    )
    assert build_scada_source(sep2.scada).backend == "ieee2030_5"


async def test_mqtt_backend_is_a_passthrough() -> None:
    source = MqttScadaSource()
    assert await source.poll_once(CollectingSink()) == 0
    await source.close()


class _Publisher:
    def __init__(self) -> None:
        self.sent: list[tuple[str, dict[str, Any], int]] = []

    async def publish(self, topic: str, payload: str, qos: int = 0) -> None:
        self.sent.append((topic, json.loads(payload), qos))


async def test_mqtt_bridge_publishes_on_existing_topics_with_schema_validation() -> None:
    publisher = _Publisher()
    sink = MqttBridgeSink(publisher, "ogtest/unit/")
    signal = ScadaBankSignal(
        bank_id="bank-003", signal="APPARENT_POWER_KVA", value=512.5, unit="kVA", quality="good", ts=NOW
    )
    instruction = ScadaUtilityInstruction(
        instruction_id=uuid4(), bank_id="bank-003", kind="LIMIT", limit_kw=400.0, issued_at=NOW, issued_by="X"
    )
    await sink.on_bank_signal(signal)
    await sink.on_utility_instruction(instruction)
    assert [(t, q) for t, _, q in publisher.sent] == [
        ("ogtest/unit/scada/bank-003", 0),
        ("ogtest/unit/scada/instruction/bank-003", 1),
    ]
    assert publisher.sent[0][1]["value"] == 512.5
    bad = signal.model_copy(update={"bank_id": ""})
    with pytest.raises(SchemaValidationError):
        await sink.on_bank_signal(bad)


async def test_fleet_sink_feeds_the_fleet_twin(monkeypatch: pytest.MonkeyPatch) -> None:
    from opengrid import fleet

    for name in ("_bank_scada", "_pending_scada", "_utility_instructions"):
        monkeypatch.setattr(fleet, name, {})  # isolate the module-level twin state

    sink = FleetScadaSink()
    await sink.on_bank_signal(
        ScadaBankSignal(
            bank_id="bank-int-1", signal="APPARENT_POWER_KVA", value=77.0, unit="kVA", quality="good", ts=NOW
        )
    )
    instruction = ScadaUtilityInstruction(
        instruction_id=uuid4(), bank_id="bank-int-1", kind="BLOCK", issued_at=NOW, issued_by="X"
    )
    await sink.on_utility_instruction(instruction)
    stored = fleet.bank_scada_signal("bank-int-1")
    assert stored is not None and stored.value == 77.0
    assert fleet.utility_instruction("bank-int-1") == instruction


def test_instruction_tracker_emits_changes_only_with_deterministic_ids() -> None:
    a = InstructionTracker(source="dnp3", issued_by="U")
    b = InstructionTracker(source="dnp3", issued_by="U")
    limit = DesiredInstruction.from_levels(estop=False, block=False, limit_kw=300.0)
    first = a.update("bank-1", limit, now=NOW)
    assert first is not None and first.kind == "LIMIT" and first.limit_kw == 300.0
    assert a.update("bank-1", limit, now=NOW + timedelta(seconds=2)) is None
    same = b.update("bank-1", limit, now=NOW)
    assert same is not None and same.instruction_id == first.instruction_id  # reconnect re-reads -> same id
    estop = a.update("bank-1", DesiredInstruction.from_levels(estop=True, block=True, limit_kw=1.0), now=NOW)
    assert estop is not None and estop.kind == "ESTOP" and estop.limit_kw is None
    lifted = a.update("bank-1", None, now=NOW)
    assert lifted is not None and lifted.expires_at == lifted.issued_at and lifted.kind == "ESTOP"
    assert a.update("bank-1", None, now=NOW) is None
    assert DesiredInstruction.from_levels(estop=False, block=False, limit_kw=-5.0) == DesiredInstruction(
        kind="LIMIT", limit_kw=0.0
    )


def test_assembler_limit_without_usable_value_fails_closed() -> None:
    points = {
        "lim": MappedPoint(bank_id="b", role="UTILITY_LIMIT_KW"),
        "act": MappedPoint(bank_id="b", role="UTILITY_LIMIT_ACTIVE"),
        "kva": MappedPoint(bank_id="b", role="APPARENT_POWER_KVA", scale=1000.0),
    }
    assembler = BankPointAssembler(points, source="t", issued_by="U")
    assembler.update_binary("act", True, online=True)
    assembler.update_analog("lim", 0.0, "missing")
    assembler.update_analog("kva", 0.4, "good")
    assert assembler.desired("b") == DesiredInstruction(kind="LIMIT", limit_kw=0.0)
    [signal] = assembler.signals(NOW)
    assert signal.value == 400.0 and signal.unit == "kVA"
    with pytest.raises(ValueError, match="unknown SCADA point role"):
        MappedPoint(bank_id="b", role="NOPE")


def test_tls_context() -> None:
    assert build_client_ssl_context(TlsSettings()) is None
    ctx = build_client_ssl_context(TlsSettings(enabled=True))
    assert ctx is not None and ctx.check_hostname
    with pytest.raises(FileNotFoundError):
        build_client_ssl_context(
            TlsSettings(enabled=True, cert_file="/nonexistent/c.pem", key_file="/nonexistent/k.pem")
        )
