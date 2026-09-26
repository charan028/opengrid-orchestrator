"""ogsim.customer -- customer-operator simulators (BUILD.md customer-sim brief).

One simulated operator per configured customer/service (PIPELINE_AC, DATA_CENTER,
ERCOT_ENERGY/AS arbitrage, DIST_DEFERRAL, PARTNER_CAPACITY), each running autonomously:
submits opportunities and service requests through the orchestrator's real customer API,
publishes closed-loop site-measurement signals over MQTT (DATA_CENTER site meter,
PIPELINE_AC corridor current), polls obligation/invoice state, and reacts to delivery and
billing. Shares no code with `opengrid` -- only the `interfaces/` JSON Schemas.
"""

from __future__ import annotations
