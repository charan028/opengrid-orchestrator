"""The one path every model call takes (issue #26 review items 1, 3, 5, 6). Owner: ui-a/ai.

Provider order is fixed: Claude first; TypeSafe only if Claude is unavailable or fails, AND
`[ai_agent].fallback_enabled` is true, AND `TYPESAFE_API_KEY` is set. With the switch off (the default)
the fallback is never called, whatever keys exist.

For every call, whichever provider serves it, this module:

* sends only a `ModelRequest` (redacted question, redacted evidence, hash of both);
* checks the budget first and refuses when it is spent (tokens, dollars, requests per minute);
* bounds the call with the per-request timeout;
* charges the tokens the provider reports, priced per model, to the budget -- failed calls included;
* returns a `ModelCall` record per attempt so the trace names the provider, model and usage.
"""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from opengrid.ai_agent.budgets import Budget, Pricing
from opengrid.ai_agent.providers import ModelProvider, ModelRequest, ProviderError, TokenUsage
from opengrid.ai_agent.types import ModelCall, ProviderName, Purpose, RouterVerdict

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
        budget: Budget,
        pricing: Pricing | None = None,
    ) -> None:
        self._primary = primary
        self._fallback = fallback
        self._fallback_enabled = fallback_enabled
        self._budget = budget
        self._pricing = pricing or Pricing()

    @property
    def budget(self) -> Budget:
        return self._budget

    def providers(self) -> list[ModelProvider]:
        """The providers that may be called, in order. The fallback is included only when switched on."""
        order: list[ModelProvider] = []
        if self._primary is not None and self._primary.available:
            order.append(self._primary)
        if self._fallback_enabled and self._fallback is not None and self._fallback.available:
            order.append(self._fallback)
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
        outcome: Outcome[T] = Outcome()
        timeout_s = self._budget.limits.timeout_s
        for provider in self.providers():
            model = provider.model_for(purpose)
            if model is None:
                continue
            refusal = self._budget.check()
            if refusal is not None:
                outcome.budget_refusal = refusal
                return outcome
            usage = TokenUsage()
            error: str | None = None
            value: T | None = None
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
            usd = self._pricing.cost_usd(
                model, input_tokens=usage.input_tokens, output_tokens=usage.output_tokens
            )
            self._budget.record(tokens=usage.input_tokens + usage.output_tokens, usd=usd)
            outcome.calls.append(
                ModelCall(
                    provider=provider.name,
                    model=model,
                    purpose=purpose,
                    input_tokens=usage.input_tokens,
                    output_tokens=usage.output_tokens,
                    usd=round(usd, 6),
                    ok=error is None,
                    error=error,
                )
            )
            if error is None and value is not None:
                outcome.value, outcome.provider, outcome.model = value, provider.name, model
                return outcome
            logger.info(
                "copilot %s via %s failed (%s); trying the next provider", purpose, provider.name, error
            )
        return outcome
