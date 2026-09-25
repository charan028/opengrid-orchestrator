"""ogsim.fleet -- simulated hub fleet (02b §4 fleet twin, §5 sim harness).

Entry point: `python -m ogsim.fleet`. Publishes telemetry, verifies and
applies signed command batches, honours leases/stops, and injects the
FLEET_* anomaly catalogue. Never imports opengrid or ogsim.market/control.
"""

from __future__ import annotations
