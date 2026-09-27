"""The one path every model call takes (issue #26 review items 1, 3, 5, 6). Owner: ui-a/ai.

Provider order: Claude first; TypeSafe only if Claude is unavailable or fails, AND
`[ai_agent].fallback_enabled` is true, AND `TYPESAFE_API_KEY` is set. With the switch off (the default)
the fallback is never called, whatever keys exist.

One deliberate exception, `[ai_agent].screening_provider = "typesafe"`: screening (intent, injection
risk, needs-trace) goes to TypeSafe's System One first, because a typed judgement model answers that
question in a few hundred milliseconds with calibrated probabilities, and Claude screens only if it
fails. Explanations are never affected: TypeSafe writes no prose, so Claude stays the explanation
provider whichever way screening is set. The default keeps Claude for both.

For every call, whichever provider serves it, this module:

* sends only a `ModelRequest` (redacted question, redacted evidence, hash of both);
* checks the budget first and refuses when it is spent (tokens, dollars, requests per minute);
* bounds the call with the per-request timeout -- for screening the shorter `screen_timeout_s` (5 s),
  retried `screen_retries` (1) times on a timeout, connection error or 5xx only;
* measures it: latency and error class per attempt (log, `og_copilot_model_call_seconds`), and screening
  outcomes in a row (`ScreeningHealth`, which drives ALR-COPILOT-SCREENING) -- never any content;
* charges the tokens the provider reports, priced per model, to the budget -- failed calls included;
* returns a `ModelCall` record per attempt so the trace names the provider, model and usage.
"""

from __future__ import annotations

import asyncio
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime

from opengrid.ai_agent.budgets import Budget, Pricing
from opengrid.ai_agent.providers import ModelProvider, ModelRequest, ProviderError, TokenUsage
from opengrid.ai_agent.types import ModelCall, ProviderName, Purpose, RouterVerdict
from opengrid.platform import metrics

#: Who screens first. "claude" is the default order; "typesafe" puts System One in front for screening only.
ScreeningProvider = ProviderName

logger = logging.getLogger(__name__)


@dataclass(slots=True)
class Outcome[T]:
    """What one gateway step produced: the value (None when nothing answered), every attempt made, and
    the budget refusal if the budget stopped it."""

    value: T | None = None
    provider: ProviderName | None = None
    model: str | None = None
    calls: list[ModelCall] = field(default_factory=list)
    budget_refusal: str | None = None


class ModelGateway:
    def __init__(
        self,
        *,
        primary: ModelProvider | None,
        fallback: ModelProvider | None = None,
        fallback_enabled: bool = False,
        screening_provider: ScreeningProvider = "claude",
        budget: Budget,
        pricing: Pricing | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_enabled = fallback_enabled
        self._screening_provider: ScreeningProvider = screening_provider
        self._budget = budget
        self._pricing = pricing or Pricing()
        self.screening = ScreeningHealth(alert_after=budget.limits.screen_alert_after)

    @property
    def budget(self) -> Budget:
        return self._budget

    def providers(self, purpose: Purpose | None = None) -> list[ModelProvider]:
        """The providers that may be called for `purpose`, in order. The fallback is included only when
        switched on, or, for screening, when it is the configured screening provider (then it goes first
        and the primary covers for it). With no purpose, the union: what can be called at all."""
        primary = self._primary if self._primary is not None and self._primary.available else None
        fallback = self._fallback if self._fallback is not None and self._fallback.available else None
        screens_with_fallback = self._screening_provider == "typesafe" and purpose in (None, "screen")
        order: list[ModelProvider] = []
        if fallback is not None and purpose == "screen" and screens_with_fallback:
            order.append(fallback)
        if primary is not None:
            order.append(primary)
        if (
            fallback is not None
            and fallback not in order
            and (self._fallback_enabled or screens_with_fallback)
        ):
            order.append(fallback)
        return order

    @property
    def available(self) -> bool:
        return bool(self.providers())

    def status(self) -> dict[str, object]:
        def describe(provider: ModelProvider | None) -> dict[str, object] | None:
            if provider is None:
                return None
            return {
                "provider": provider.name,
                "available": provider.available,
                "screen_model": provider.model_for("screen"),
                "explain_model": provider.model_for("explain"),
            }

        return {
            "primary": describe(self._primary),
            "fallback": describe(self._fallback),
            "fallback_enabled": self._fallback_enabled,
            "screening_provider": self._screening_provider,
            "screening": self.screening.snapshot(),
        }

    async def screen(self, request: ModelRequest) -> Outcome[RouterVerdict]:
        return await self._run("screen", request, lambda p, r, t: p.screen(r, timeout_s=t))

    async def explain(self, request: ModelRequest) -> Outcome[str]:
        return await self._run("explain", request, lambda p, r, t: p.explain(r, timeout_s=t))

    async def _run[T](
        self,
        purpose: Purpose,
        request: ModelRequest,
        invoke: Callable[[ModelProvider, ModelRequest, float], Awaitable[tuple[T, TokenUsage]]],
    ) -> Outcome[T]:
        outcome: Outcome[T] = await self._attempts(purpose, request, invoke)
        if purpose == "screen" and outcome.budget_refusal is None:
            # A budget refusal is policy, not a failure of the model path: it is not counted.
            last = outcome.calls[-1] if outcome.calls else None
            self.screening.record(ok=outcome.value is not None, error=None if last is None else last.error)
        return outcome

    async def _attempts[T](
        self,
        purpose: Purpose,
        request: ModelRequest,
        invoke: Callable[[ModelProvider, ModelRequest, float], Awaitable[tuple[T, TokenUsage]]],
    ) -> Outcome[T]:
        outcome: Outcome[T] = Outcome()
        limits = self._budget.limits
        timeout_s = limits.screen_timeout_s if purpose == "screen" else limits.timeout_s
        tries = 1 + (limits.screen_retries if purpose == "screen" else 0)
        for provider in self.providers(purpose):
            model = provider.model_for(purpose)
            if model is None:
                continue
            for attempt in range(1, tries + 1):
                refusal = self._budget.check()
                if refusal is not None:
                    outcome.budget_refusal = refusal
                    return outcome
                call, value = await self._attempt(
                    provider, model, purpose, request, invoke, timeout_s, attempt
                )
                outcome.calls.append(call)
                if call.ok and value is not None:
                    outcome.value, outcome.provider, outcome.model = value, provider.name, model
                    return outcome
                if error_class(call.error) not in RETRYABLE:
                    break
            logger.info(
                "copilot %s via %s failed (%s); trying the next provider",
                purpose,
                provider.name,
                error_class(outcome.calls[-1].error) if outcome.calls else "no call",
            )
        return outcome

    async def _attempt[T](
        self,
        provider: ModelProvider,
        model: str,
        purpose: Purpose,
        request: ModelRequest,
        invoke: Callable[[ModelProvider, ModelRequest, float], Awaitable[tuple[T, TokenUsage]]],
        timeout_s: float,
        attempt: int,
    ) -> tuple[ModelCall, T | None]:
        """One bounded, charged, measured call. Logged by latency and error class only -- never the
        question, the evidence or the model's text."""
        usage = TokenUsage()
        error: str | None = None
        value: T | None = None
        started = time.monotonic()
        try:
            async with asyncio.timeout(timeout_s):
                value, usage = await invoke(provider, request, timeout_s)
        except ProviderError as exc:
            error, usage = exc.reason, exc.usage
        except TimeoutError:
            error = f"{provider.name}: timeout"
        except Exception as exc:
            # A provider bug must not become an operator-facing error; it is traced by type only.
            error = f"{provider.name}: {type(exc).__name__}"
            logger.warning("copilot provider %s raised unexpectedly", provider.name, exc_info=True)
        latency_s = time.monotonic() - started
        usd = self._pricing.cost_usd(
            model, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
        )
        self._budget.record(tokens=usage.input_tokens + usage.output_tokens, usd=usd)
        outcome_label = error_class(error) if error is not None else "ok"
        metrics.copilot_model_call_seconds.labels(purpose=purpose, outcome=outcome_label).observe(latency_s)
        logger.info(
            "copilot model call",
            extra={
                "purpose": purpose,
                "provider": provider.name,
                "model": model,
                "attempt": attempt,
                "latency_ms": round(latency_s * 1000),
                "timeout_s": timeout_s,
                "outcome": outcome_label,
                "input_tokens": usage.input_tokens,
                "output_tokens": usage.output_tokens,
            },
        )
        call = ModelCall(
            provider=provider.name,
            model=model,
            purpose=purpose,
            input_tokens=usage.input_tokens,
            output_tokens=usage.output_tokens,
            usd=round(usd, 6),
            ok=error is None,
            error=error,
            latency_ms=round(latency_s * 1000),
            attempt=attempt,
        )
        return call, value


#: Error classes worth one more try: the request may simply not have reached the model. A rate limit,
#: an auth or request error, or a refusal would fail the same way again.
RETRYABLE = frozenset({"timeout", "connection_error"}) | frozenset(f"http_{code}" for code in range(500, 600))


def error_class(error: str | None) -> str:
    """ "claude: timeout" -> "timeout". The class only: provider errors never carry request content."""
    if not error:
        return "ok"
    return error.split(":", 1)[-1].strip() or "error"


@dataclass(slots=True)
class ScreeningHealth:
    """Screening outcomes in this process (after retries): what System Health and ALR-COPILOT-SCREENING
    read. Screening failing means every answer falls back to the no-model tier."""

    alert_after: int = 3
    screenings: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    last_error: str | None = None
    last_ok_at: datetime | None = None
    last_failure_at: datetime | None = None

    @property
    def alerting(self) -> bool:
        return self.consecutive_failures >= self.alert_after

    def record(self, *, ok: bool, error: str | None, now: datetime | None = None) -> None:
        now = now or datetime.now(UTC)
        was_alerting = self.alerting
        self.screenings += 1
        if ok:
            self.consecutive_failures = 0
            self.last_ok_at = now
        else:
            self.failures += 1
            self.consecutive_failures += 1
            self.last_error = error_class(error)
            self.last_failure_at = now
            metrics.copilot_screening_failures_total.labels(error=self.last_error).inc()
        metrics.copilot_screening_consecutive_failures.set(self.consecutive_failures)
        if self.alerting and not was_alerting:
            logger.warning(
                "copilot screening failing: %d in a row (last error class: %s); answers fall back to the "
                "no-model tier",
                self.consecutive_failures,
                self.last_error,
            )

    def snapshot(self) -> dict[str, object]:
        return {
            "screenings": self.screenings,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "alert_after": self.alert_after,
            "alerting": self.alerting,
            "last_error": self.last_error,
            "last_ok_at": self.last_ok_at.isoformat() if self.last_ok_at else None,
            "last_failure_at": self.last_failure_at.isoformat() if self.last_failure_at else None,
        }
