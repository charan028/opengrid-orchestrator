"""Guardian's independently-read input ports for K14 (07-delivery/06-service-profiles-and-power-quality.md
S5.3/S6.7, G-21..G-25) -- new file, additive to `opengrid.guardian.ports` (WP-C, not editing that module;
the live-path agent wires `GuardianPorts`/`GuardianService` together, see this package's README for the
exact lines to add).

Same discipline as `guardian.ports`: the guardian must never trust the allocator's/asset-health ladder's
claim that an envelope holds or that a hub has been substituted off a sensitive delivery -- it re-reads
the measured PQ pipeline, the per-hub asset characterization and the ledger itself. `Protocol`s are the
seams; a real process wires Postgres/MQTT-backed adapters, tests use in-memory fakes, mirroring
`opengrid.guardian.ports`'s own "pure logic separated from I/O" split (BUILD.md S5a).
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Literal, Protocol
from uuid import UUID

from opengrid.core.models.pq import CalibrationReference
from opengrid.core.pq import CalibrationBounds, OffsetVector, PqEnvelopeLimits, PqMeasurement

AssetState = Literal["OK", "WATCH", "DEGRADED", "QUARANTINED", "AWAITING_REPLACEMENT", "RECOMMISSIONING"]


@dataclass(frozen=True, slots=True)
class ProposedCalibrationCommand:
    """The unsigned candidate `CalibrationCommand` content G-25 evaluates -- mirrors `guardian.ports.
    ProposedBatch`'s role for `CommandBatch`. Built from a `PENDING` `og.calibration_attempt` row the
    ladder (`opengrid.assets`) recorded; guardian never re-derives the correction, only re-checks it
    (defense in depth, S6.7). `epoch`/`seq` are deliberately absent: the guardian assigns them itself
    when it signs (per-hub strictly increasing, `GuardianService.evaluate_and_sign_calibration`)."""

    hub_id: str
    correction: OffsetVector
    bounds: CalibrationBounds
    calibration_id: UUID
    reference: CalibrationReference
    issued_at: datetime
    expires_at: datetime


@dataclass(frozen=True, slots=True)
class HubAssetSnapshot:
    """Guardian's own, independently-read view of one hub's asset-health characterization (S5.5.2-3),
    never the ladder's claim about what state a hub is in."""

    asset_state: AssetState
    ride_through_class: str


class PqEnvelopeStatePort(Protocol):
    async def tightest_active_limits(self, bank_id: str) -> PqEnvelopeLimits | None:
        """The tightest `PowerQualityEnvelope` among obligations currently served behind `bank_id`
        (S5.3 G-21..G-23: "compare to the tightest active limit among obligations served behind it").
        `None` when no obligation behind this bank carries a non-default envelope -- callers treat that
        as "nothing to check" (grid-code-minimum dispatch is unconstrained by K14, S4.c)."""
        ...


class PqMeasurementPort(Protocol):
    async def aggregate_measurement(self, bank_id: str) -> tuple[PqMeasurement, bool]:
        """`(measurement, is_stale)` -- the measured aggregate PQ reading for `bank_id` (S6.5 step 3,
        `og.pq_waveform_summary`-derived), or the S3.2 modelled fallback from `og.hub_inverter_pq`
        characterization with `is_stale=True` when no measured summary is fresh enough (S4.a's
        freshness gate, the same "stale -> exclude/fallback" pattern as K1/G-01). Guardian's G-21..G-23
        treat a stale measurement conservatively (never as silently compliant)."""
        ...


class HubAssetStatePort(Protocol):
    async def snapshot(self, hub_id: str) -> HubAssetSnapshot | None: ...


class CalibrationHistoryPort(Protocol):
    async def last_attempt_epoch_s(self, hub_id: str) -> float | None:
        """Epoch seconds of the last calibration command the GUARDIAN ITSELF signed for `hub_id`, or
        `None` if it never signed one -- feeds G-25's rate limit (S5.5.4, default 1/24h). Read from the
        guardian's own signed-verdict trace, never from the ladder's `og.calibration_attempt` rows: the
        ladder records its candidate before the guardian evaluates it, so that table's newest row is
        always the very command under evaluation (which would refuse every calibration)."""
        ...


class FirmwareCalibrationBoundsPort(Protocol):
    async def max_bounds_for_hub(self, hub_id: str) -> CalibrationBounds:
        """The inverter model/firmware family's configured maximum correction magnitudes for `hub_id`
        (S6.7's "defense in depth" bound, independent of whatever bound the candidate command itself
        claims)."""
        ...


class SensitiveGrantPort(Protocol):
    async def has_active_non_default_envelope_grant(self, hub_id: str) -> bool:
        """G-25(iii)/S5.5.4: does `hub_id` currently carry an active committed grant for a non-default-
        envelope (PQ-sensitive, e.g. `DATA_CENTER`/`PIPELINE_AC`) obligation, read fresh from the ledger?
        Never assumes the ladder's step-2 substitution already ran -- that is exactly the gap G-25
        exists to close (S5.5.4: "enforced by the new guardian check reading the ledger, not by
        convention")."""
        ...
