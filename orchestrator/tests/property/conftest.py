"""Hypothesis profile shared by the K1-K13 property tests (no wall-clock deadlines: CI hosts vary)."""

from __future__ import annotations

from hypothesis import HealthCheck, settings

settings.register_profile(
    "opengrid",
    max_examples=100,
    deadline=None,
    suppress_health_check=[HealthCheck.too_slow],
)
settings.load_profile("opengrid")
