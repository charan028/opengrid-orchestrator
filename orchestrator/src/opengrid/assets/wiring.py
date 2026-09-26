"""One place that builds a Postgres-backed `AssetHealthService` for a host process (live-path wiring,
README "Wiring"): og-settle runs the drift sweep with it, og-engine handles calibration acks with it.
`AssetHealthService` holds no in-process state beyond its ports, so each process builds its own."""

from __future__ import annotations

from psycopg_pool import AsyncConnectionPool

from opengrid.assets.repo import (
    PgAssetEventRepo,
    PgAssetHealthRepo,
    PgCalibrationAttemptRepo,
    PgDriftObservationRepo,
    PgSensitiveGrantPort,
    PgWorkOrderRepo,
    TraceStoreAssetTracePort,
)
from opengrid.assets.service import AssetHealthPorts, AssetHealthService
from opengrid.trace import TraceStore


def build_asset_health_service(pool: AsyncConnectionPool, trace_store: TraceStore) -> AssetHealthService:
    return AssetHealthService(
        ports=AssetHealthPorts(
            drift=PgDriftObservationRepo(pool),
            asset_health=PgAssetHealthRepo(pool),
            calibration_attempts=PgCalibrationAttemptRepo(pool),
            work_orders=PgWorkOrderRepo(pool),
            asset_events=PgAssetEventRepo(pool),
            trace=TraceStoreAssetTracePort(trace_store),
            sensitive_grants=PgSensitiveGrantPort(pool),
        )
    )
