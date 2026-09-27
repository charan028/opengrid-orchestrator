"""Utility tolling contracts: the daily reservation (decision log D-29, issue #36; 08 S3; 09 S11).

A tolling contract is `service_type = REGULATED_CAPACITY`, `variant = TOLLING` (Austin Energy RCA 26-1526
terms: a fixed $/kW-yr capacity price, the utility calls discharge in its window). For R3 the standing
reservation is ONE OBLIGATION PER DAY for the utility's window (`[contracts.tolling]`, America/Chicago
wall clock), sized to the contract's product rule. A true contract-term reservation follows after R3.

This module only creates `OFFERED` opportunities through the normal admission path
(`opengrid.contracts.admission.admit_priced`). The selector then commits each one as a regulated capacity
hold (stage R, 09 D3); the allocator holds it at 0 kW until the utility calls it; settle pays it on
availability. Nothing here selects, commits, reserves or dispatches.

Idempotent: `ContractsRepo.find_opportunity_by_window` is checked before every admission, so running it
at every gate (or twice a day) never creates a duplicate. It never offers a window that overlaps the same
contract's COMMITTED/DELIVERING obligation (K13), and never offers a window that has already started.

`plan_windows` and `tolling_config_from` are pure; `run_tolling_contract`/`run_tolling` do I/O through the
injected repo and trace store.
"""

from __future__ import annotations

import logging
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import UTC, date, datetime, time, timedelta
from decimal import Decimal
from typing import Any, Literal

from opengrid.contracts.admission import admit_priced
from opengrid.contracts.errors import AdmissionError
from opengrid.contracts.repository import ContractsRepo
from opengrid.core.models.engine import Contract, Opportunity, ProductRule
from opengrid.core.models.market import Utility, UtilityId
from opengrid.core.services import REGULATED_CAPACITY_SERVICE_TYPE
from opengrid.core.timeutil import MARKET_TZ
from opengrid.market.capacity import capacity_value_usd_per_mwh
from opengrid.market.config import DEFAULT_UTILITIES
from opengrid.trace import TraceStore

logger = logging.getLogger(__name__)

TOLLING_SERVICE_TYPE = REGULATED_CAPACITY_SERVICE_TYPE
TOLLING_VARIANT = "TOLLING"
TRACE_EVENT = "TOLLING"

#: Planning default window (D-29: about 1.5 h, the utility's evening peak), America/Chicago.
DEFAULT_WINDOW_START = time(16, 30)
DEFAULT_WINDOW_END = time(18, 0)
DEFAULT_DAYS_AHEAD = 1

DaysKind = Literal["ALL", "WEEKDAYS"]


@dataclass(frozen=True, slots=True)
class TollingConfig:
    """`[contracts.tolling]`: the utility's daily call window (local wall clock) and how far ahead to
    reserve. `days_ahead = 1` offers today's window (if not yet started) and tomorrow's, so the DA gate
    always sees the next window."""

    window_start_local: time = DEFAULT_WINDOW_START
    window_end_local: time = DEFAULT_WINDOW_END
    days_ahead: int = DEFAULT_DAYS_AHEAD
    days: DaysKind = "ALL"

    def __post_init__(self) -> None:
        if self.window_end_local <= self.window_start_local:
            raise ValueError("[contracts.tolling]: window_end must be after window_start (same local day)")
        if self.days_ahead < 0:
            raise ValueError("[contracts.tolling]: days_ahead must be >= 0")

    @property
    def window_minutes(self) -> int:
        start = self.window_start_local.hour * 60 + self.window_start_local.minute
        end = self.window_end_local.hour * 60 + self.window_end_local.minute
        return end - start


def _parse_hhmm(value: object, key: str) -> time:
    try:
        return time.fromisoformat(str(value))
    except ValueError as exc:
        raise ValueError(f"[contracts.tolling].{key}: expected HH:MM, got {value!r}") from exc


def tolling_config_from(cfg: object) -> TollingConfig:
    """`[contracts.tolling]`: `window_start` / `window_end` ("HH:MM", America/Chicago), `days_ahead`
    (default 1), `days` ("ALL" or "WEEKDAYS"). `cfg` is duck-typed (`.get(dotted_key, default)`,
    i.e. `opengrid.platform.config.Config`). A malformed value raises: no silent default."""

    def get(key: str, default: Any) -> Any:
        return cfg.get(f"contracts.tolling.{key}", default) if hasattr(cfg, "get") else default

    days = str(get("days", "ALL"))
    if days not in ("ALL", "WEEKDAYS"):
        raise ValueError(f"[contracts.tolling].days: expected ALL or WEEKDAYS, got {days!r}")
    return TollingConfig(
        window_start_local=_parse_hhmm(
            get("window_start", DEFAULT_WINDOW_START.isoformat("minutes")), "window_start"
        ),
        window_end_local=_parse_hhmm(
            get("window_end", DEFAULT_WINDOW_END.isoformat("minutes")), "window_end"
        ),
        days_ahead=int(get("days_ahead", DEFAULT_DAYS_AHEAD)),
        days="WEEKDAYS" if days == "WEEKDAYS" else "ALL",
    )


def is_tolling_contract(contract: Contract) -> bool:
    return contract.service_type == TOLLING_SERVICE_TYPE and contract.variant == TOLLING_VARIANT


def _window_on(day: date, config: TollingConfig) -> tuple[datetime, datetime]:
    """The window on local calendar `day`, as UTC. Built from the local wall clock, so it keeps its
    local times across a DST change."""
    start = datetime.combine(day, config.window_start_local, tzinfo=MARKET_TZ)
    end = datetime.combine(day, config.window_end_local, tzinfo=MARKET_TZ)
    return start.astimezone(UTC), end.astimezone(UTC)


def plan_windows(now: datetime, config: TollingConfig) -> list[tuple[datetime, datetime]]:
    """The windows to reserve at `now`: today's (local) if it has not started yet, plus the next
    `days_ahead` days'; weekends skipped when `days = WEEKDAYS`. Pure."""
    today = now.astimezone(MARKET_TZ).date()
    out: list[tuple[datetime, datetime]] = []
    for offset in range(config.days_ahead + 1):
        day = today + timedelta(days=offset)
        if config.days == "WEEKDAYS" and day.weekday() >= 5:
            continue
        start, end = _window_on(day, config)
        if start > now:
            out.append((start, end))
    return out


def _tolling_rule(rules: list[ProductRule]) -> ProductRule | None:
    return rules[0] if rules else None


async def _overlaps_commitment(
    repo: ContractsRepo, contract: Contract, start: datetime, end: datetime
) -> bool:
    active = await repo.active_obligations_by_interval(
        start, end, service_type=contract.service_type, states=("COMMITTED", "DELIVERING")
    )
    return any(o.contract_id == contract.contract_id for o in active)


async def run_tolling_contract(
    repo: ContractsRepo,
    trace: TraceStore,
    contract: Contract,
    *,
    now: datetime,
    config: TollingConfig,
    utilities: Mapping[UtilityId, Utility] | None = None,
) -> list[Opportunity]:
    """Offer `contract`'s daily windows (`plan_windows`) through admission. Returns the opportunities
    created by this call (empty when every window already exists). A contract that is not an ACTIVE
    TOLLING contract, or whose product rule does not fix a positive block size equal to the window's
    duration, creates nothing and is logged (fail closed: never a guessed size or window)."""
    if not is_tolling_contract(contract) or contract.status != "ACTIVE":
        return []
    rule = _tolling_rule(await repo.get_product_rules(contract.contract_id))
    if rule is None or rule.min_qty_kw <= 0:
        logger.error(
            "tolling contract has no sized product rule; nothing reserved",
            extra={"contract_id": str(contract.contract_id)},
        )
        return []
    if rule.duration_minutes != config.window_minutes:
        logger.error(
            "tolling window does not match the product rule duration; nothing reserved",
            extra={
                "contract_id": str(contract.contract_id),
                "rule_minutes": rule.duration_minutes,
                "window_minutes": config.window_minutes,
            },
        )
        return []

    utility_map = DEFAULT_UTILITIES if utilities is None else utilities
    utility = utility_map.get(contract.utility_id) if contract.utility_id is not None else None
    if utility is None:
        logger.warning(
            "tolling contract has no known utility terms; opportunity admitted at value 0 "
            "(the selector and settle price the capacity from og.utility)",
            extra={"contract_id": str(contract.contract_id), "utility_id": contract.utility_id},
        )
        value = Decimal("0")
    else:
        value = capacity_value_usd_per_mwh(utility.capacity_price_usd_per_kw, utility.payment_basis)

    created: list[Opportunity] = []
    for start, end in plan_windows(now, config):
        if await repo.find_opportunity_by_window(contract.contract_id, start, end) is not None:
            continue  # idempotent
        if await _overlaps_commitment(repo, contract, start, end):
            continue  # K13: already committed, never re-offered
        try:
            opportunity = await admit_priced(
                repo, trace, contract.contract_id, start, end, rule.min_qty_kw, value_per_mwh=value
            )
        except AdmissionError as exc:
            logger.warning(
                "tolling window rejected at admission",
                extra={
                    "contract_id": str(contract.contract_id),
                    "window_start": start.isoformat(),
                    "reason": str(exc),
                },
            )
            continue
        await trace.append(
            f"tolling-{contract.contract_id}",
            "ADMISSION",
            TRACE_EVENT,
            {
                "contract_id": str(contract.contract_id),
                "opportunity_id": str(opportunity.opportunity_id),
                "window_start": start.isoformat(),
                "window_end": end.isoformat(),
                "requested_kw": str(rule.min_qty_kw),
                "value_per_mwh": str(value),
            },
            None,
        )
        created.append(opportunity)
    return created


async def run_tolling(
    repo: ContractsRepo,
    trace: TraceStore,
    *,
    config: TollingConfig,
    now: datetime | None = None,
    utilities: Mapping[UtilityId, Utility] | None = None,
) -> list[Opportunity]:
    """`run_tolling_contract` for every ACTIVE tolling contract. One contract's failure never blocks
    another's (logged)."""
    now = now or datetime.now(UTC)
    created: list[Opportunity] = []
    for contract in await repo.list_contracts(service_type=TOLLING_SERVICE_TYPE, status="ACTIVE"):
        if not is_tolling_contract(contract):
            continue
        try:
            created.extend(
                await run_tolling_contract(repo, trace, contract, now=now, config=config, utilities=utilities)
            )
        except Exception:
            logger.exception(
                "tolling reservation failed for contract", extra={"contract_id": str(contract.contract_id)}
            )
    return created
