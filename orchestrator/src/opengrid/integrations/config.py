"""`[integrations]` configuration and the backend factories (protocol-adapters.md S7).

    [integrations.scada]
    backend = "mqtt"            # "mqtt" (default) | "dnp3" | "iccp" | "ieee2030_5"
    sink = "mqtt_bridge"        # where a protocol backend delivers: "mqtt_bridge" | "fleet"

    [integrations.market]
    backend = "none"            # "none" (default) | "ercot_mms"
    enabled = false             # must ALSO be true before anything is ever submitted to a market

With no `[integrations]` table at all, the result is exactly today's behaviour: MQTT SCADA, no market
submission. A protocol backend is only constructed (no sockets, no certificate loading) when selected.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, model_validator

from opengrid.integrations.ercot_mms.client import ErcotMmsSettings
from opengrid.integrations.ercot_mms.intake import AwardContractMap
from opengrid.integrations.ieee2030_5.adapter import Ieee20305Settings
from opengrid.integrations.interfaces import MarketSubmission, ScadaSource
from opengrid.integrations.scada_dnp3.adapter import Dnp3Settings
from opengrid.integrations.scada_iccp.adapter import IccpSettings

__all__ = [
    "IntegrationsSettings",
    "MarketIntegrationSettings",
    "ScadaIntegrationSettings",
    "build_market_submission",
    "build_scada_source",
    "load_integrations_settings",
]


class ScadaIntegrationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["mqtt", "dnp3", "iccp", "ieee2030_5"] = "mqtt"
    sink: Literal["mqtt_bridge", "fleet"] = "mqtt_bridge"
    dnp3: Dnp3Settings | None = None
    iccp: IccpSettings | None = None
    ieee2030_5: Ieee20305Settings | None = None

    @model_validator(mode="after")
    def _backend_configured(self) -> ScadaIntegrationSettings:
        if self.backend != "mqtt" and getattr(self, self.backend) is None:
            raise ValueError(
                f"[integrations.scada].backend = {self.backend!r} needs [integrations.scada.{self.backend}]"
            )
        return self


class MarketIntegrationSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    backend: Literal["none", "ercot_mms"] = "none"
    enabled: bool = False
    ercot_mms: ErcotMmsSettings | None = None
    contracts: AwardContractMap = AwardContractMap()

    @model_validator(mode="after")
    def _backend_configured(self) -> MarketIntegrationSettings:
        if self.backend == "ercot_mms" and self.ercot_mms is None:
            raise ValueError(
                "[integrations.market].backend = 'ercot_mms' needs [integrations.market.ercot_mms]"
            )
        return self


class IntegrationsSettings(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    scada: ScadaIntegrationSettings = ScadaIntegrationSettings()
    market: MarketIntegrationSettings = MarketIntegrationSettings()


def load_integrations_settings(raw: Mapping[str, Any] | None) -> IntegrationsSettings:
    """Validate the `[integrations]` table (`cfg.get("integrations")`); `None` -> all defaults."""
    return IntegrationsSettings.model_validate(dict(raw or {}))


def build_scada_source(settings: ScadaIntegrationSettings) -> ScadaSource:
    if settings.backend == "dnp3" and settings.dnp3 is not None:
        from opengrid.integrations.scada_dnp3.adapter import Dnp3ScadaSource

        return Dnp3ScadaSource(settings.dnp3)
    if settings.backend == "iccp" and settings.iccp is not None:
        from opengrid.integrations.scada_iccp.adapter import IccpScadaSource

        return IccpScadaSource(settings.iccp)
    if settings.backend == "ieee2030_5" and settings.ieee2030_5 is not None:
        from opengrid.integrations.ieee2030_5.adapter import Ieee20305ScadaSource

        return Ieee20305ScadaSource(settings.ieee2030_5)
    from opengrid.integrations.mqtt_backend import MqttScadaSource

    return MqttScadaSource()


def build_market_submission(settings: MarketIntegrationSettings) -> MarketSubmission | None:
    """`None` unless a backend is chosen AND `enabled = true` (default OFF: nothing is ever submitted)."""
    if settings.backend == "none" or not settings.enabled or settings.ercot_mms is None:
        return None
    from opengrid.integrations.ercot_mms.client import ErcotMmsClient

    return ErcotMmsClient(settings.ercot_mms)
