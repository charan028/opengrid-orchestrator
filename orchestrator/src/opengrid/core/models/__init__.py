"""Pydantic v2 contracts: wire (MQTT/API) + DB row shapes, shared by every process (02b S1.1, S12).

Re-exported as `opengrid.contracts` is NOT done for MVP-S (no such alias module exists); import directly
from `opengrid.core.models.<mqtt|engine|platform|market>` per BUILD.md's actual repo layout.
"""

from opengrid.core.models import engine, market, mqtt, platform, pq

__all__ = ["engine", "market", "mqtt", "platform", "pq"]
