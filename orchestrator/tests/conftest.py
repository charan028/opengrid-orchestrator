"""Shared pytest fixtures for orchestrator unit/property tests. No DB/MQTT here -- those belong to
tests/integration (owned by respective module agents)."""

from __future__ import annotations

import sys
from pathlib import Path

# Ensure `src` is importable without requiring `pip install -e .` (BUILD.md S5 PYTHONPATH fallback).
_SRC = Path(__file__).resolve().parents[1] / "src"
if str(_SRC) not in sys.path:
    sys.path.insert(0, str(_SRC))
