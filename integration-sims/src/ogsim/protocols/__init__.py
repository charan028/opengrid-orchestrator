"""ogsim.protocols -- local simulators for real utility and market protocols (protocol-adapters.md).

Each simulator speaks the real wire protocol (or, for ICCP, the documented simulator transport) so the
orchestrator's production adapters can be tested end-to-end without a utility or ERCOT:

- `dnp3_outstation`: DNP3 outstation (TCP) serving the same bank points as `ogsim.scada`;
- `ieee2030_5_server`: minimal IEEE 2030.5 server (FastAPI, `application/sep+xml`);
- `iccp_server`: ICCP/TASE.2 bilateral-table data exchange over the simulator TCP transport;
- `ercot_mms`: ERCOT MMS-style QSE submission endpoint (SOAP/XML) that clears offers into awards.

Like every ogsim package these share no code with `opengrid` (BUILD.md S1); the wire contracts are
documented in docs/orchestrator/07-delivery/integrations/protocol-adapters.md.
"""
