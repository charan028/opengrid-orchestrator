"""opengrid.assets -- asset-health state machine and remote-calibration workflow (07-delivery/06-
service-profiles-and-power-quality.md S5.5, S6.7; WP-I, 09.2 Agent I).

Owns: the `AssetState` lifecycle (`state_machine.py`), the calibration-command builder and outcome
classification glue (`calibration.py`, built on `opengrid.core.pq.calibration`'s pure math -- never
re-implemented here), the maintenance-work-order/asset-event record shapes and orchestration
(`service.py`), and Postgres persistence for `og.hub_inverter_pq`'s asset-health columns plus
`og.calibration_attempt`/`og.maintenance_work_order`/`og.asset_event` (`repo.py`, migration `0011_
asset_health.sql`, owned by Agent A -- this package only reads/writes those tables, it does not alter
their schema).

Terminology (binding, front matter item 7 of the spec): *substitution* is the allocator's software
dispatch action of moving delivery to different hubs; *inverter swap / replacement* is a physical
hardware action recorded as `og.asset_event(event_type='INVERTER_REPLACED')`. Never conflated here.
"""

from __future__ import annotations
