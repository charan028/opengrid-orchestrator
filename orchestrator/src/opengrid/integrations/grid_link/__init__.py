"""opengrid.integrations.grid_link -- the utility grid-control link (decision log D-34; spec
docs/orchestrator/07-delivery/integrations/grid-link.md).

A utility's control system (Austin Energy first) sends D-29 TOLLING discharge calls and L2 LIMIT/BLOCK
levels, and reads availability, delivery, call state, SoC and alarms back, over a control-system protocol.

Layers (protocol-neutral core, protocol at the edge):

- `model`, `service`, `l2`, `ports`: what a command means and what happens to it; knows no protocol;
- `points`, `dnp3_session`, `dnp3_server`: DNP3 (IEEE 1815) outstation, layout "opengrid-gridlink-v1",
  on the in-house codec `opengrid.integrations.scada_dnp3.codec` (no third-party DNP3 package);
- `calls_port`: the shared core toll-call function `opengrid.calls` (never duplicated here);
- `fleet_telemetry`: per-bank figures from the fleet twin;
- `runner`: started by og-engine when `[grid_link]` enables a utility (disabled by default).

An ICCP/TASE.2 transport can be added beside `dnp3_server` by mapping a bilateral table's data values onto
the same `GridCommand`s and `LinkStatus` (grid-link.md S8); the service does not change.
"""

from opengrid.integrations.grid_link.config import (
    GridLinkSettings,
    UtilityLinkSettings,
    load_grid_link_settings,
)
from opengrid.integrations.grid_link.model import CallPhase, ControlVerdict, GridCommand, LinkStatus

__all__ = [
    "CallPhase",
    "ControlVerdict",
    "GridCommand",
    "GridLinkSettings",
    "LinkStatus",
    "UtilityLinkSettings",
    "load_grid_link_settings",
]
