"""`load_guardian_config` reads `[guardian]`/`[allocator]` with sane defaults, no hard-coded thresholds
in `checks.py`/`service.py` (BUILD.md S5a "Config: no hard-coded... thresholds")."""

from __future__ import annotations

from opengrid.guardian.config import DEFAULT_CLOCK_OFFSET_MAX_MS, GuardianConfig, load_guardian_config
from opengrid.platform.config import Config


def test_defaults_when_section_absent():
    cfg = Config({})
    guardian_cfg = load_guardian_config(cfg)
    assert guardian_cfg.clock_offset_max_ms == DEFAULT_CLOCK_OFFSET_MAX_MS
    assert guardian_cfg.key_path == "/etc/opengrid/guardian_ed25519.key"
    assert guardian_cfg.as_release_enabled is False


def test_reads_configured_values():
    cfg = Config(
        {
            "guardian": {
                "key_path": "/etc/opengrid-test/guardian.key",
                "clock_offset_max_ms": 50,
                "verdict_timeout_ms": 100,
                "as_release_enabled": True,
                "key_id": "guardian-custom",
            },
            "allocator": {"cycle_interval_s": 5},
        }
    )
    guardian_cfg = load_guardian_config(cfg)
    assert guardian_cfg.key_path == "/etc/opengrid-test/guardian.key"
    assert guardian_cfg.clock_offset_max_ms == 50.0
    assert guardian_cfg.verdict_timeout_ms == 100.0
    assert guardian_cfg.as_release_enabled is True
    assert guardian_cfg.key_id == "guardian-custom"
    assert guardian_cfg.cycle_interval_s == 5.0


def test_guardian_config_is_frozen():
    cfg = GuardianConfig(key_path="x")
    try:
        cfg.key_path = "y"  # type: ignore[misc]
    except AttributeError:
        pass
    else:
        raise AssertionError("GuardianConfig must be immutable")
