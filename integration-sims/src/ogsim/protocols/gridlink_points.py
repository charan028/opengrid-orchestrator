"""ogsim's own copy of the grid-link point layout "opengrid-gridlink-v1"
(interfaces/grid_link/opengrid-gridlink-v1.json). ogsim shares no code with opengrid; a test asserts
these constants equal the interfaces file."""

from __future__ import annotations

__all__ = [
    "AI",
    "AI_BANK_BASE",
    "AI_BANK_STRIDE",
    "AI_TARGET_BASE",
    "AO",
    "AO_TARGET_BASE",
    "BI",
    "BI_TARGET_BASE",
    "BO",
    "BO_TARGET_BASE",
    "CALL_REASONS",
    "CALL_STATES",
    "CROB_LATCH_OFF",
    "CROB_LATCH_ON",
    "CROB_PULSE_ON",
    "FLAG_ONLINE",
    "STATUS",
]

AO = {"CALL_SETPOINT_KW": 0, "CALL_DURATION_MIN": 1, "CALL_ID": 2}
AO_TARGET_BASE = 16
BO = {"CALL_EXECUTE": 0, "CALL_CANCEL": 1, "HEARTBEAT": 2}
BO_TARGET_BASE = 16  # + 2t: L2_LIMIT_ACTIVE, + 2t + 1: L2_BLOCK
AI = {
    "AVAILABLE_KW": 0,
    "DELIVERED_KW": 1,
    "CALL_STATE": 2,
    "CALL_ID": 3,
    "CALL_REASON": 4,
    "CALL_DELIVERED_KW": 5,
    "SOC_PCT": 6,
    "HEARTBEAT_COUNT": 7,
}
AI_TARGET_BASE = 16
AI_BANK_BASE = 100
AI_BANK_STRIDE = 4
BI = {
    "LINK_HEALTHY": 0,
    "CALL_ACTIVE": 1,
    "ALARM_HEARTBEAT_LOST": 2,
    "ALARM_CALL_REJECTED": 3,
    "ALARM_TELEMETRY_STALE": 4,
    "ALARM_L2_ACTIVE": 5,
    "TOLL_CALLS_ENABLED": 6,
}
BI_TARGET_BASE = 16
CALL_STATES = {0: "IDLE", 1: "ACCEPTED", 2: "ACTIVE", 3: "ENDED", 4: "REJECTED"}
STATUS = {
    "SUCCESS": 0,
    "TIMEOUT": 1,
    "NO_SELECT": 2,
    "FORMAT_ERROR": 3,
    "NOT_SUPPORTED": 4,
    "NOT_AUTHORIZED": 9,
    "AUTOMATION_INHIBIT": 10,
    "OUT_OF_RANGE": 12,
}
CALL_REASONS = {
    1: "R-CALL-NOT-FOUND",
    2: "R-CALL-NOT-DEPLOYABLE",
    3: "R-CALL-STATE",
    4: "R-CALL-NO-PRODUCT-DURATION",
    5: "R-CALL-DURATION-CAP",
    6: "R-CALL-OVERLAP",
    7: "R-CALL-CHARGE-REFUSED",
    8: "R-CALL-OVER-COMMITTED",
    9: "R-CALL-OUTSIDE-WINDOW",
    10: "R-CALL-IDEMPOTENCY-CONFLICT",
    11: "R-CALL-RATE-LIMIT",
    12: "R-CALL-FLEET-WIDE",
    50: "R-GL-LINK-DOWN",
    51: "R-GL-CORE-TIMEOUT",
    52: "R-GL-INTERNAL",
    99: "UNKNOWN",
}
FLAG_ONLINE = 0x01
CROB_PULSE_ON = 0x01
CROB_LATCH_ON = 0x03
CROB_LATCH_OFF = 0x04
