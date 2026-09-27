"""`[firmware]` configuration (R3.1). Every value has a safe default so a missing section only means
"no catalogue entries from config" -- a campaign still needs a catalogued target to exist at all.

    [firmware]
    soc_margin_pct = 10.0            # SoC must be >= reserve + this % of usable capacity
    committed_lookahead_s = 900      # "next update window" for the committed-obligation check
    command_lease_s = 120            # the hub must accept a command within this lease
    update_timeout_s = 600           # UPDATING longer than this without DONE = transient failure
    max_attempts = 3                 # per hub, transient failures only
    retry_initial_s = 60
    retry_max_s = 900
    refusal_defer_s = 60             # a guardian refusal (e.g. SoC) re-checks after this
    second_operator_hub_threshold = 50
    bank_max_concurrent_pct = 10.0
    feeder_max_concurrent_pct = 10.0
    max_failures = 2
    max_failure_pct = 5.0
    max_issue_skew_s = 5.0

    [[firmware.catalogue]]
    version = "1.5.0"
    hardware_revision = "revB"
    sha256 = "<64 hex>"
    release_note = "..."
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from opengrid.platform.config import Config


@dataclass(frozen=True, slots=True)
class FirmwareConfig:
    soc_margin_pct: float = 10.0
    committed_lookahead_s: float = 900.0
    command_lease_s: float = 120.0
    update_timeout_s: float = 600.0
    max_attempts: int = 3
    retry_initial_s: float = 60.0
    retry_max_s: float = 900.0
    refusal_defer_s: float = 60.0
    second_operator_hub_threshold: int = 50
    bank_max_concurrent_pct: float = 10.0
    feeder_max_concurrent_pct: float = 10.0
    max_failures: int = 2
    max_failure_pct: float = 5.0
    max_issue_skew_s: float = 5.0
    catalogue: tuple[dict[str, Any], ...] = ()


def load_firmware_config(cfg: Config) -> FirmwareConfig:
    section = cfg.get("firmware", {}) or {}
    defaults = FirmwareConfig()

    def num(key: str, default: float) -> float:
        return float(section.get(key, default))

    return FirmwareConfig(
        soc_margin_pct=num("soc_margin_pct", defaults.soc_margin_pct),
        committed_lookahead_s=num("committed_lookahead_s", defaults.committed_lookahead_s),
        command_lease_s=num("command_lease_s", defaults.command_lease_s),
        update_timeout_s=num("update_timeout_s", defaults.update_timeout_s),
        max_attempts=int(section.get("max_attempts", defaults.max_attempts)),
        retry_initial_s=num("retry_initial_s", defaults.retry_initial_s),
        retry_max_s=num("retry_max_s", defaults.retry_max_s),
        refusal_defer_s=num("refusal_defer_s", defaults.refusal_defer_s),
        second_operator_hub_threshold=int(
            section.get("second_operator_hub_threshold", defaults.second_operator_hub_threshold)
        ),
        bank_max_concurrent_pct=num("bank_max_concurrent_pct", defaults.bank_max_concurrent_pct),
        feeder_max_concurrent_pct=num("feeder_max_concurrent_pct", defaults.feeder_max_concurrent_pct),
        max_failures=int(section.get("max_failures", defaults.max_failures)),
        max_failure_pct=num("max_failure_pct", defaults.max_failure_pct),
        max_issue_skew_s=num("max_issue_skew_s", defaults.max_issue_skew_s),
        catalogue=tuple(dict(entry) for entry in section.get("catalogue", []) or []),
    )
