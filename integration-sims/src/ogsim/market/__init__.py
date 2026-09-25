"""ogsim.market: simulates ERCOT Public API, EIA v2 and NWS for the orchestrator's `feeds` process.

Runs on port 8090 (`python -m ogsim.market`). Data modes: replay (from the
2.5-day history TSV) or synthetic (diurnal curves + noise). Anomalies are
injected through the internal admin API (`/admin/...`), normally driven by
`ogsim.control`.
"""
