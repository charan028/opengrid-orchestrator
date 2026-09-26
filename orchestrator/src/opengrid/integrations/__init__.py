"""opengrid.integrations -- production adapters for real utility SCADA protocols and ERCOT market
submission, behind two interfaces (docs/orchestrator/07-delivery/integrations/protocol-adapters.md).

- `ScadaSource` backends: `mqtt` (default, the existing topics), `dnp3`, `iccp`, `ieee2030_5`;
- `MarketSubmission` backends: `ercot_mms` (OFF by default).

Selection is by `[integrations]` config (`opengrid.integrations.config`). Every backend is tested end to
end against a local ogsim simulator; moving to a real endpoint needs credentials, certificates and the
counterparty's point list / bilateral table -- not code. This package shares no code with `ogsim`.
"""

from opengrid.integrations.interfaces import (
    AsOffer,
    Award,
    DispatchInstruction,
    EnergyOffer,
    MarketSubmission,
    OfferCurvePoint,
    ScadaSink,
    ScadaSource,
    SubmissionReceipt,
    ThreePartSupplyOffer,
)

__all__ = [
    "AsOffer",
    "Award",
    "DispatchInstruction",
    "EnergyOffer",
    "MarketSubmission",
    "OfferCurvePoint",
    "ScadaSink",
    "ScadaSource",
    "SubmissionReceipt",
    "ThreePartSupplyOffer",
]
