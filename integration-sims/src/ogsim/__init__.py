"""ogsim: independent OpenGrid integration simulators.

This package must never import from `opengrid` (the orchestrator). It stands
in for external systems (ERCOT/EIA/NWS, utility SCADA, the battery fleet) and
their shared anomaly-injection control plane.
"""

__version__ = "0.1.0"
