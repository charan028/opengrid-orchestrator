"""Appends every injection to a JSONL log so QA can correlate it with the
orchestrator's response. Prefers /var/lib/opengrid/sim/anomalies.jsonl,
falling back to ./anomalies.jsonl when that path isn't writable (e.g. local
dev off the server). `OGSIM_ANOMALY_LOG_PATH` overrides both, for tests and
for operators who want the log somewhere else."""

from __future__ import annotations

import json
import os
import threading
import time
from pathlib import Path
from typing import Any

PREFERRED_PATH = "/var/lib/opengrid/sim/anomalies.jsonl"
FALLBACK_PATH = "./anomalies.jsonl"
PATH_ENV_VAR = "OGSIM_ANOMALY_LOG_PATH"

_lock = threading.Lock()


def _resolve_path(explicit: str | None) -> Path:
    if explicit:
        return Path(explicit)
    env_override = os.environ.get(PATH_ENV_VAR)
    if env_override:
        return Path(env_override)
    preferred = Path(PREFERRED_PATH)
    try:
        preferred.parent.mkdir(parents=True, exist_ok=True)
        probe = preferred.parent / ".ogsim_write_probe"
        probe.write_text("", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return preferred
    except OSError:
        return Path(FALLBACK_PATH)


class AnomalyLog:
    def __init__(self, path: str | None = None):
        self.path = _resolve_path(path)

    def append(self, record: dict[str, Any]) -> None:
        record = {"logged_at": time.time(), **record}
        line = json.dumps(record, default=str)
        with _lock, self.path.open("a", encoding="utf-8") as f:
            f.write(line + "\n")

    def read_all(self) -> list[dict[str, Any]]:
        if not self.path.exists():
            return []
        out = []
        with self.path.open("r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        return out
