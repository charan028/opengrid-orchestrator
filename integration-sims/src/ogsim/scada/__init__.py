"""ogsim.scada -- simulated utility SCADA (02b §4/§5.1 bank aggregators).

Entry point: `python -m ogsim.scada`. Aggregates fleet telemetry per bank
into a simulated kVA reading plus background load, publishes
`scada_bank_signal`, issues rule-based or scenario-driven utility
instructions, and injects the SCADA_* anomaly catalogue. Never imports
opengrid or ogsim.market/control.
"""

from __future__ import annotations
