# opengrid.integrations.grid_link

The utility grid-control link (decision D-34). Spec: `docs/orchestrator/07-delivery/integrations/grid-link.md`.

**Purpose.** A utility EMS (Austin Energy first) sends D-29 toll calls and L2 LIMIT/BLOCK over DNP3 (IEEE
1815, mutual TLS). It reads available/delivered kW, call state, SoC and alarms back. The link runs inside
og-engine and is disabled by default (`[grid_link]`).

**Interface.**

- `runner.start_grid_link(cfg, pool, trace)` is the only entry point, called by og-engine. It returns
  `None` when the link is disabled.
- `service.GridLinkService` is protocol-neutral:
  - transports call `offer(command, peer)` / `validate(command)` / `deny(...)` and read `status()`;
  - `run()` is the worker.
- `dnp3_server.Dnp3GridLinkServer` and `dnp3_session.OutstationSession` are the DNP3 transport. The point
  layout is `points.py`, matching `interfaces/grid_link/opengrid-gridlink-v1.json`.
- `calls_port.CoreTollCallPort` adapts `opengrid.calls`, the one core call function shared with the operator
  route and the customer API. Call logic is never duplicated here.

**How to test.**

```
cd orchestrator && python -m pytest tests/unit/integrations/grid_link -q
cd integration-sims && python -m pytest tests/test_protocols_gridlink_master.py tests/test_scada_grid_link.py -q
```

The loopback tests open localhost ephemeral ports only. The TLS test writes a throw-away PKI under pytest's
temp directory.
