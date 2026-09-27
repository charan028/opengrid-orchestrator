"""opengrid.delivery -- measured delivery verification of every discharge call (D-38; one owner).

`opengrid.core.delivery` is the pure computation. This package discovers the calls (utility toll calls
and ERCOT AS deployments from `og.as_deployment`, operator manual discharge targets from MANUAL_TARGET
trace rows), reads their aligned telemetry/command/meter series (`series`), persists `og.delivery_record`
(`store`, migration 0050) and runs og-settle's live job with its alerts and AT_RISK flag (`job`).
Other modules read delivery only through `store.fetch_record`, `store.list_records` and
`store.fetch_live_points`.
"""
