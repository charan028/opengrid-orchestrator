"""ogsim.fleet.firmware -- the hub side of a guardian-signed FIRMWARE_UPDATE (R3.1).

Implements `interfaces/mqtt/firmware_command.schema.json` (inbound, `<root>/cmd/fw/<hub_id>`) and
`firmware_status.schema.json` (outbound, `<root>/ack/fw/<hub_id>`) on ogsim's own per-hub state. Pure logic,
no MQTT, mirroring `ogsim.fleet.calibration`: verify the signature, then freshness (lease window, strictly
increasing (epoch, seq) per hub -- K6), then hub-level checks, then act.

An accepted update runs as a timeline driven by `tick(now)`: ACCEPTED -> DOWNLOADING -> INSTALLING ->
REBOOTING -> DONE, over a duration drawn from `[duration_min_s, duration_max_s]` (default 30-90 s).
While a hub is updating it publishes no telemetry (`is_updating`); after the reboot the runtime
republishes its device-info with the new `firmware_version` (`take_device_info_changes`).

Failures (`failure_rate`, default 0; the demo sets 0.1) are drawn once per accepted command from
`failure_kinds`: a DOWNLOAD_ERROR/VERIFY_ERROR aborts before install (transient; the orchestrator retries), an
INSTALL_ERROR/BOOT_FAILED reverts to the previous version (A/B partition) and reports FAILED (terminal). A
command whose sha256 does not match the image the hub downloads is HASH_MISMATCH, and one for another
hardware revision is HARDWARE_INCOMPATIBLE -- both terminal, both refused before anything is installed. A
ROLLBACK command installs its (older) target the same way.

Every status message carries the hub's own `ts` (owner addition 1).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

import numpy as np
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PublicKey

from ogsim.common.crypto import verify_signature
from ogsim.fleet.commands import parse_rfc3339, utc_now_from_epoch

_SIGNED_EXCLUDED = ("key_id", "signature")
_PHASES = (("DOWNLOADING", 0.0), ("INSTALLING", 0.4), ("REBOOTING", 0.8))
_PRE_INSTALL_FAILURES = frozenset({"DOWNLOAD_ERROR", "VERIFY_ERROR"})


@dataclass(frozen=True)
class FirmwareSimConfig:
    duration_min_s: float = 30.0
    duration_max_s: float = 90.0
    failure_rate: float = 0.0
    failure_kinds: tuple[str, ...] = ("INSTALL_ERROR", "DOWNLOAD_ERROR")
    #: version -> sha256 of the image the hub would download. Empty = the hub trusts the command's hash
    #: (no mismatch possible); a version missing from a non-empty map is a DOWNLOAD_ERROR (404).
    images: dict[str, str] = field(default_factory=dict)
    default_version: str = "1.4.2"
    default_hardware_revision: str = "revB"


@dataclass
class _Update:
    command_id: str
    epoch: int
    seq: int
    target_version: str
    started_at: float
    duration_s: float
    failure: str | None
    phase: str = "ACCEPTED"


@dataclass
class _HubFirmware:
    version: str
    hardware_revision: str
    last_epoch: int = -1
    last_seq: int = -1
    update: _Update | None = None


def _iso(now: float) -> str:
    return datetime.fromtimestamp(now, UTC).isoformat().replace("+00:00", "Z")


def verify_firmware_signature(command: dict[str, Any], public_key: Ed25519PublicKey) -> bool:
    """Ed25519 over JCS(every field except key_id/signature), exactly what the guardian signed."""
    fields = {k: v for k, v in command.items() if k not in _SIGNED_EXCLUDED}
    return verify_signature(public_key, fields, command.get("signature"))


class FirmwareManager:
    """Per-hub firmware state for the simulated fleet."""

    def __init__(
        self,
        hub_ids: list[str],
        config: FirmwareSimConfig | None = None,
        *,
        rng: np.random.Generator | None = None,
        versions: dict[str, str] | None = None,
        hardware_revisions: dict[str, str] | None = None,
    ) -> None:
        self.config = config or FirmwareSimConfig()
        self._rng = rng if rng is not None else np.random.default_rng(0)
        self._hubs = {
            hub_id: _HubFirmware(
                version=(versions or {}).get(hub_id, self.config.default_version),
                hardware_revision=(hardware_revisions or {}).get(
                    hub_id, self.config.default_hardware_revision
                ),
            )
            for hub_id in hub_ids
        }
        self._device_info_changed: set[str] = set()

    # ------------------------------------------------------------------ queries

    def version(self, hub_id: str) -> str | None:
        hub = self._hubs.get(hub_id)
        return hub.version if hub else None

    def hardware_revision(self, hub_id: str) -> str | None:
        hub = self._hubs.get(hub_id)
        return hub.hardware_revision if hub else None

    def is_updating(self, hub_id: str) -> bool:
        """True while the hub is mid-update: the runtime publishes no telemetry for it."""
        hub = self._hubs.get(hub_id)
        return hub is not None and hub.update is not None

    def device_info_fields(self, hub_id: str) -> dict[str, str]:
        """The firmware fields of the hub's device_info message (merge into the runtime's builder)."""
        hub = self._hubs[hub_id]
        return {"firmware_version": hub.version, "hardware_revision": hub.hardware_revision}

    def take_device_info_changes(self) -> list[str]:
        """Hubs whose running version changed since the last call (republish their device_info)."""
        changed = sorted(self._device_info_changed)
        self._device_info_changed.clear()
        return changed

    # ------------------------------------------------------------------ inbound

    def handle_command(
        self, command: dict[str, Any], public_key: Ed25519PublicKey, now: float
    ) -> dict[str, Any]:
        """Verify and (maybe) start one FirmwareCommand. Returns the status message to publish: ACCEPTED,
        REJECTED (never acted on) or FAILED (refused on the image: hash/hardware)."""
        hub_id = str(command.get("hub_id", ""))
        hub = self._hubs.get(hub_id)
        if hub is None:
            return self._status(command, "REJECTED", "UNKNOWN_HUB", now, version="unknown")
        if not verify_firmware_signature(command, public_key):
            return self._status(command, "REJECTED", "BAD_SIGNATURE", now, version=hub.version)
        try:
            issued_at = parse_rfc3339(command["issued_at"])
            expires_at = parse_rfc3339(command["expires_at"])
            epoch, seq = int(command["epoch"]), int(command["seq"])
        except (KeyError, TypeError, ValueError):
            return self._status(command, "REJECTED", "EXPIRED", now, version=hub.version)
        if not (issued_at <= utc_now_from_epoch(now) < expires_at):
            return self._status(command, "REJECTED", "EXPIRED", now, version=hub.version)
        if (epoch, seq) <= (hub.last_epoch, hub.last_seq):
            return self._status(command, "REJECTED", "STALE_SEQ", now, version=hub.version)
        if hub.update is not None:
            return self._status(command, "REJECTED", "ALREADY_UPDATING", now, version=hub.version)
        hub.last_epoch, hub.last_seq = epoch, seq

        target = str(command["target_version"])
        if str(command.get("hardware_revision")) != hub.hardware_revision:
            return self._status(command, "FAILED", "HARDWARE_INCOMPATIBLE", now, version=hub.version)
        images = self.config.images
        if images:
            if target not in images:
                return self._status(command, "FAILED", "DOWNLOAD_ERROR", now, version=hub.version)
            if images[target] != command.get("sha256"):
                return self._status(command, "FAILED", "HASH_MISMATCH", now, version=hub.version)

        failure: str | None = None
        if self.config.failure_rate > 0 and self._rng.random() < self.config.failure_rate:
            kinds = self.config.failure_kinds or ("INSTALL_ERROR",)
            failure = str(kinds[int(self._rng.integers(0, len(kinds)))])
        duration = float(self._rng.uniform(self.config.duration_min_s, self.config.duration_max_s))
        hub.update = _Update(str(command["command_id"]), epoch, seq, target, now, duration, failure)
        return self._status(command, "ACCEPTED", None, now, version=hub.version, progress=0.0)

    # ------------------------------------------------------------------ timeline

    def tick(self, now: float) -> list[dict[str, Any]]:
        """Advance every in-progress update; returns the status messages to publish (one per phase change)."""
        messages: list[dict[str, Any]] = []
        for hub_id, hub in self._hubs.items():
            update = hub.update
            if update is None:
                continue
            fraction = (now - update.started_at) / update.duration_s if update.duration_s > 0 else 1.0
            if update.failure in _PRE_INSTALL_FAILURES and fraction >= _PHASES[1][1]:
                messages.append(self._finish(hub_id, hub, update, now, "FAILED", update.failure))
                continue
            if fraction >= 1.0:
                if update.failure is not None:
                    messages.append(self._finish(hub_id, hub, update, now, "FAILED", update.failure))
                else:
                    hub.version = update.target_version
                    self._device_info_changed.add(hub_id)
                    messages.append(self._finish(hub_id, hub, update, now, "DONE", None))
                continue
            phase = [name for name, start in _PHASES if fraction >= start][-1]
            if phase != update.phase:
                update.phase = phase
                messages.append(
                    self._message(
                        hub_id, update, phase, None, now, version=hub.version, progress=100.0 * fraction
                    )
                )
        return messages

    def _finish(
        self, hub_id: str, hub: _HubFirmware, update: _Update, now: float, state: str, reason: str | None
    ) -> dict[str, Any]:
        hub.update = None
        if state == "FAILED" and reason in ("INSTALL_ERROR", "BOOT_FAILED"):
            self._device_info_changed.add(hub_id)  # rebooted back onto the previous partition
        return self._message(hub_id, update, state, reason, now, version=hub.version, progress=100.0)

    # ------------------------------------------------------------------ messages

    @staticmethod
    def _message(
        hub_id: str,
        update: _Update,
        state: str,
        reason: str | None,
        now: float,
        *,
        version: str,
        progress: float,
    ) -> dict[str, Any]:
        return {
            "hub_id": hub_id,
            "command_id": update.command_id,
            "epoch": update.epoch,
            "seq": update.seq,
            "state": state,
            "reason": reason,
            "progress_pct": round(min(100.0, max(0.0, progress)), 1),
            "firmware_version": version,
            "target_version": update.target_version,
            "ts": _iso(now),
        }

    @staticmethod
    def _status(
        command: dict[str, Any],
        state: str,
        reason: str | None,
        now: float,
        *,
        version: str,
        progress: float = 0.0,
    ) -> dict[str, Any]:
        message: dict[str, Any] = {
            "hub_id": str(command.get("hub_id", "")) or "unknown",
            "command_id": str(command.get("command_id", "00000000-0000-0000-0000-000000000000")),
            "state": state,
            "reason": reason,
            "progress_pct": progress,
            "firmware_version": version,
            "target_version": str(command.get("target_version", "")) or "unknown",
            "ts": _iso(now),
        }
        for name in ("epoch", "seq"):
            value = command.get(name)
            if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
                message[name] = value
        return message
