"""Per-message CPU of MQTT telemetry validation on the ingest path: the reference `jsonschema` validator alone vs
`platform.mqtt.validate_payload` with the precompiled accept-check (`platform.fast_schema`), for valid telemetry
(the steady state) and an invalid message (which the reference still judges).

    python tests-perf/bench_validate.py
"""

from __future__ import annotations

import contextlib
import sys
import time
from pathlib import Path
from typing import Any

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "orchestrator" / "src"))

from opengrid.platform import mqtt

_MIN = {
    "hub_id": "hub-00001",
    "bank_id": "bank-001",
    "zone": "LZ_NORTH",
    "ts": "2026-09-27T03:00:00+00:00",
    "soc_kwh": 20.5,
    "p_kw": -3.2,
    "health": "online",
    "seq": 7,
    "epoch": 1,
}
_FLOW = {
    **_MIN,
    "home_load_kw": 1.2,
    "meter_kw": -2.0,
    "cell_temp_c": 31.5,
    "p_dis_max_kw": 11.0,
    "lat": 32.7,
}


def _us(fn: Any, payload: dict[str, Any], n: int) -> float:
    best = float("inf")
    for _ in range(5):
        t0 = time.perf_counter()
        for _ in range(n):
            with contextlib.suppress(mqtt.SchemaValidationError):
                fn(payload)
        best = min(best, (time.perf_counter() - t0) / n * 1e6)
    return best


def main() -> int:
    reference = mqtt._validator_for("telemetry")

    def _old(payload: dict[str, Any]) -> None:
        try:
            reference.validate(instance=payload)
        except Exception as exc:
            raise mqtt.SchemaValidationError(str(exc)) from exc

    def _new(payload: dict[str, Any]) -> None:
        mqtt.validate_payload("telemetry", payload)

    print(f"{'payload':<28} {'jsonschema us':>14} {'with pre-check us':>18}")
    for name, payload, n in (
        ("valid (required only)", _MIN, 3_000),
        ("valid (flow fields)", _FLOW, 3_000),
        ("invalid (bad enum)", {**_MIN, "health": "x"}, 1_000),
    ):
        print(f"{name:<28} {_us(_old, payload, n):>14.1f} {_us(_new, payload, n):>18.1f}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
