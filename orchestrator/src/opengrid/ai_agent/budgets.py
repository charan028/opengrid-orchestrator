"""Token, cost, rate and timeout budgets for the agent (issue #26 item 6). Owner: ui-a/ai.

The console must never be slowed by the assistant, so every limit here fails *closed and fast*: when a
budget is spent the request is refused in microseconds rather than queued behind a model call. All
limits come from `[ai_agent]` in the orchestrator config -- nothing is hard-coded except the safety
ceiling on the per-request timeout, which exists so a misconfiguration cannot hang an operator\'s panel.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, date, datetime

from opengrid.ai_agent.types import ConfigReader

#: A request may never be allowed to run longer than this, whatever the config says (issue #26: <= 20 s).
TIMEOUT_CEILING_S = 20.0
#: At most this many screening retries, whatever the config says (each one is charged and rate-limited).
SCREEN_RETRIES_CEILING = 2


@dataclass(frozen=True, slots=True)
class BudgetLimits:
    """The configured ceilings. `daily_usd` of 0 disables the agent entirely, which is a valid way to
    turn it off in production without removing the deployment."""

    daily_tokens: int = 200_000
    daily_usd: float = 5.0
    requests_per_minute: int = 20
    timeout_s: float = 20.0
    #: Screening is one small tool call on the fast model: it gets a short timeout and is retried (on a
    #: timeout, connection error or 5xx only), so one slow attempt costs seconds, not the whole 20 s.
    screen_timeout_s: float = 5.0
    screen_retries: int = 1
    #: Consecutive failed screenings (after retries) that raise the ALR-COPILOT-SCREENING warning.
    screen_alert_after: int = 3

    @classmethod
    def from_config(cls, cfg: ConfigReader) -> BudgetLimits:
        """Build from anything with a `.get(dotted_path, default)`, i.e. `opengrid.platform.config.Config`."""
        read = cfg.get
        timeout_s = min(float(read("ai_agent.timeout_s", 20.0)), TIMEOUT_CEILING_S)
        return cls(
            daily_tokens=int(read("ai_agent.daily_tokens", 200_000)),
            daily_usd=float(read("ai_agent.daily_usd", 5.0)),
            requests_per_minute=int(read("ai_agent.requests_per_minute", 20)),
            timeout_s=timeout_s,
            screen_timeout_s=min(float(read("ai_agent.screen_timeout_s", 5.0)), timeout_s),
            screen_retries=max(0, min(int(read("ai_agent.screen_retries", 1)), SCREEN_RETRIES_CEILING)),
            screen_alert_after=max(1, int(read("ai_agent.screen_alert_after", 3))),
        )


@dataclass(frozen=True, slots=True)
class ModelPrice:
    """US dollars per million tokens, as billed by the provider."""

    input_per_mtok: float
    output_per_mtok: float


#: What an unpriced model is charged at: deliberately above every configured price, so a model someone
#: forgot to price exhausts the dollar budget early instead of running for free.
UNPRICED = ModelPrice(input_per_mtok=25.0, output_per_mtok=125.0)


class Pricing:
    """Turns the token usage a provider reports into dollars, from `[ai_agent.prices.<model>]`."""

    def __init__(self, prices: dict[str, ModelPrice] | None = None) -> None:
        self._prices = dict(prices or {})

    @classmethod
    def from_config(cls, cfg: ConfigReader) -> Pricing:
        raw = cfg.get("ai_agent.prices", {}) or {}
        prices: dict[str, ModelPrice] = {}
        if isinstance(raw, dict):
            for model, entry in raw.items():
                if isinstance(entry, dict):
                    prices[str(model)] = ModelPrice(
                        input_per_mtok=float(entry.get("input_per_mtok", UNPRICED.input_per_mtok)),
                        output_per_mtok=float(entry.get("output_per_mtok", UNPRICED.output_per_mtok)),
                    )
        return cls(prices)

    def price_of(self, model: str) -> ModelPrice:
        return self._prices.get(model, UNPRICED)

    def cost_usd(self, model: str, *, input_tokens: int, output_tokens: int) -> float:
        price = self.price_of(model)
        return (
            max(input_tokens, 0) * price.input_per_mtok + max(output_tokens, 0) * price.output_per_mtok
        ) / 1_000_000.0


@dataclass(slots=True)
class BudgetState:
    """What has been spent today. Process-local on purpose: og-api is one process per host, and a
    cross-host budget would need a shared store this MVP does not have -- documented, not hidden."""

    day: date = field(default_factory=lambda: datetime.now(UTC).date())
    tokens: int = 0
    usd: float = 0.0
    recent: list[float] = field(default_factory=list)


class Budget:
    """Tracks spend and answers one question: may this request run right now, and why not."""

    def __init__(self, limits: BudgetLimits) -> None:
        self._limits = limits
        self._state = BudgetState()

    @property
    def limits(self) -> BudgetLimits:
        return self._limits

    def _roll_day(self, now: datetime) -> None:
        if now.date() != self._state.day:
            self._state = BudgetState(day=now.date())

    def check(self, *, now: datetime | None = None) -> str | None:
        """None when the request may proceed, else a plain-language reason it may not."""
        now = now or datetime.now(UTC)
        self._roll_day(now)
        if self._limits.daily_usd <= 0:
            return "the assistant is switched off in this deployment"
        if self._state.tokens >= self._limits.daily_tokens:
            return "today's token budget for the assistant is spent"
        if self._state.usd >= self._limits.daily_usd:
            return "today's cost budget for the assistant is spent"
        cutoff = now.timestamp() - 60.0
        self._state.recent = [t for t in self._state.recent if t >= cutoff]
        if len(self._state.recent) >= self._limits.requests_per_minute:
            return "too many assistant requests in the last minute"
        return None

    def record(self, *, tokens: int, usd: float = 0.0, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        self._roll_day(now)
        self._state.tokens += max(tokens, 0)
        self._state.usd += max(usd, 0.0)
        self._state.recent.append(now.timestamp())

    def headroom(self, *, now: datetime | None = None) -> dict[str, float | int | str]:
        """What System Health shows (UI-DAT-05): what is left, not just what was spent."""
        now = now or datetime.now(UTC)
        self._roll_day(now)
        return {
            "tokens_used": self._state.tokens,
            "tokens_limit": self._limits.daily_tokens,
            "tokens_left_pct": round(
                100.0
                * max(0, self._limits.daily_tokens - self._state.tokens)
                / max(self._limits.daily_tokens, 1),
                1,
            ),
            "usd_used": round(self._state.usd, 4),
            "usd_limit": self._limits.daily_usd,
            "requests_last_minute": len(self._state.recent),
            "requests_per_minute_limit": self._limits.requests_per_minute,
            "timeout_s": self._limits.timeout_s,
            "screen_timeout_s": self._limits.screen_timeout_s,
            "screen_retries": self._limits.screen_retries,
            "day": self._state.day.isoformat(),
        }
