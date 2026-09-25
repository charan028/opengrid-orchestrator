"""ogsim.common -- shared building blocks for ogsim.fleet and ogsim.scada only.

Owns: MQTT client wrapper, injectable clock, YAML+env config loading,
JCS canonicalization and Ed25519 verification. Never imported by
ogsim.market or ogsim.control (they have their own equivalents); never
imports opengrid.
"""

from __future__ import annotations
