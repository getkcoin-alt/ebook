"""Model providers: Anthropic and OpenAI, behind one protocol.

Same shape as the payment gateways and the notification channels — one interface, one
registry, no branching on provider name anywhere else.

**Token counts come from the provider's response, never from an estimate.** Every
cost figure on the platform is derived from them, and a client-side token estimate is
wrong by 10-30% depending on the tokeniser. When a provider does not return usage,
the request is recorded with zero tokens and a warning, which is honest — a made-up
number in a cost ledger is worse than a missing one.

**A provider failure is not fatal.** The registry tries each configured provider in
order, so one being rate-limited degrades quality rather than removing the feature.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol

import httpx

from knowledgeos_core import ServiceUnavailableError, UpstreamError, get_logger
from settings import Settings

logger = get_logger(__name__)

#: Published per-million-token prices, USD. Kept here rather than fetched because a
#: cost ceiling that depends on a network call is a cost ceiling that fails open.
#:
#: These go stale. They are used for *estimates and budget enforcement*, never for
#: billing anyone, and the number shown in the UI says "estimated" for that reason.
PRICING: dict[str, tuple[float, float]] = {
    # model prefix -> (input per 1M, output per 1M)
    "claude-opus": (15.00, 75.00),
    "claude-sonnet": (3.00, 15.00),
    "claude-haiku": (0.80, 4.00),
    "gpt-4o-mini": (0.15, 0.60),
    "gpt-4o": (2.50, 10.00),
    "text-embedding-3-small": (0.02, 0.0),
    "text-embedding-3-large": (0.13, 0.0),
}

#: What an unrecognised model is assumed to cost. Deliberately on the high side: an
#: unknown model that is cheaper than this just leaves budget unspent, while an
#: unknown model assumed cheap could blow through the ceiling before anyone notices.
DEFAULT_PRICING = (5.00, 20.00)


def estimate_cost(model: str, input_tokens: int, output_tokens: int) -> float:
    """Cost in USD from published pricing. An estimate, and labelled as one."""
    rates = DEFAULT_PRICING
    for prefix, price in PRICING.items():
        if model.startswith(prefix):
            rates = price
            break
    return round((input_tokens / 1_000_000) * rates[0] + (output_tokens / 1_000_000) * rates[1], 6)


@dataclass(slots=True)
class Completion:
    """One model response."""

    content: str
    provider: str
    model: str
    input_tokens: int = 0
    output_tokens: int = 0
    #: True when the provider returned no usage block. The cost is then unknown, not
    #: zero, and the caller logs it rather than pretending the request was free.
    usage_missing: bool = False
    raw: dict[str, Any] = field(default_factory=dict)

    @property
    def cost_usd(self) -> float:
        return estimate_cost(self.model, self.input_tokens, self.output_tokens)


class ModelProvider(Protocol):
    name: str

    @property
    def configured(self) -> bool: ...

    @property
    def model(self) -> str: ...

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float = 0.7
    ) -> Completion: ...

    async def aclose(self) -> None: ...


class AnthropicProvider:
    name = "anthropic"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.anthropic_base_url,
            timeout=httpx.Timeout(settings.request_timeout, connect=10.0),
            headers={
                "x-api-key": settings.anthropic_api_key or "",
                # Pinned: a version bump changes response shapes, and discovering
                # that through a production 500 is avoidable.
                "anthropic-version": "2023-06-01",
                "content-type": "application/json",
            },
        )

    @property
    def configured(self) -> bool:
        return bool(self._settings.anthropic_api_key)

    @property
    def model(self) -> str:
        return self._settings.anthropic_model

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float = 0.7
    ) -> Completion:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            # The system prompt is a separate field, not a message. Putting it in the
            # message list is the mistake that makes a prompt overridable by input.
            "system": system,
            "messages": [{"role": "user", "content": user}],
        }
        try:
            response = await self._client.post("/v1/messages", json=payload)
        except httpx.HTTPError as exc:
            raise UpstreamError(
                "Could not reach Anthropic.", details={"upstream": "anthropic"}
            ) from exc

        if not response.is_success:
            _raise_for(response, "anthropic")

        data = response.json()
        blocks = data.get("content") or []
        text = "".join(block.get("text", "") for block in blocks if block.get("type") == "text")
        usage = data.get("usage") or {}
        return Completion(
            content=text,
            provider=self.name,
            model=str(data.get("model") or self.model),
            input_tokens=int(usage.get("input_tokens", 0)),
            output_tokens=int(usage.get("output_tokens", 0)),
            usage_missing=not usage,
            raw=data,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class OpenAIProvider:
    name = "openai"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=settings.openai_base_url,
            timeout=httpx.Timeout(settings.request_timeout, connect=10.0),
            headers={
                "Authorization": f"Bearer {settings.openai_api_key or ''}",
                "Content-Type": "application/json",
            },
        )

    @property
    def configured(self) -> bool:
        return bool(self._settings.openai_api_key)

    @property
    def model(self) -> str:
        return self._settings.openai_model

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float = 0.7
    ) -> Completion:
        payload = {
            "model": self.model,
            "max_tokens": max_tokens,
            "temperature": temperature,
            "messages": [
                {"role": "system", "content": system},
                {"role": "user", "content": user},
            ],
        }
        try:
            response = await self._client.post("/chat/completions", json=payload)
        except httpx.HTTPError as exc:
            raise UpstreamError("Could not reach OpenAI.", details={"upstream": "openai"}) from exc

        if not response.is_success:
            _raise_for(response, "openai")

        data = response.json()
        choices = data.get("choices") or [{}]
        text = (choices[0].get("message") or {}).get("content") or ""
        usage = data.get("usage") or {}
        return Completion(
            content=text,
            provider=self.name,
            model=str(data.get("model") or self.model),
            input_tokens=int(usage.get("prompt_tokens", 0)),
            output_tokens=int(usage.get("completion_tokens", 0)),
            usage_missing=not usage,
            raw=data,
        )

    async def embed(
        self, texts: list[str], model: str | None = None
    ) -> tuple[list[list[float]], int, str]:
        """Vectors for the search service's optional semantic mode."""
        target = model or self._settings.embedding_model
        try:
            response = await self._client.post(
                "/embeddings", json={"model": target, "input": texts}
            )
        except httpx.HTTPError as exc:
            raise UpstreamError("Could not reach OpenAI.", details={"upstream": "openai"}) from exc

        if not response.is_success:
            _raise_for(response, "openai")

        data = response.json()
        # Sorted by index: the API does not guarantee response order matches input
        # order, and a silently shuffled batch produces embeddings attached to the
        # wrong documents — which looks like the model being bad at its job.
        rows = sorted(data.get("data") or [], key=lambda item: item.get("index", 0))
        vectors = [list(row.get("embedding") or []) for row in rows]
        tokens = int((data.get("usage") or {}).get("prompt_tokens", 0))
        return vectors, tokens, target

    async def aclose(self) -> None:
        await self._client.aclose()


def _raise_for(response: httpx.Response, provider: str) -> None:
    """Turn a provider error into the platform's error, preserving what it said."""
    try:
        payload = response.json()
        error = payload.get("error") or {}
        message = error.get("message") or "The model provider rejected the request."
        code = str(error.get("type") or error.get("code") or "provider_error")
    except Exception:
        message, code = "The model provider rejected the request.", "provider_error"

    logger.warning(
        "ai.provider_error", provider=provider, status=response.status_code, message=message
    )
    # 429 and 5xx are worth retrying against another provider; a 400 is our bug and
    # retrying it elsewhere just spends money to fail twice.
    if response.status_code == 429 or response.status_code >= 500:
        raise ServiceUnavailableError(
            message, code=code, details={"provider": provider, "retryable": True}
        )
    raise UpstreamError(message, code=code, status_code=502, details={"provider": provider})


class ProviderRegistry:
    """Providers in preference order, with failover."""

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._providers: dict[str, ModelProvider] = {}
        if settings.anthropic_enabled:
            self._providers["anthropic"] = AnthropicProvider(settings)
        if settings.openai_enabled:
            self._providers["openai"] = OpenAIProvider(settings)

        logger.info(
            "ai.providers_ready",
            providers=list(self._providers),
            order=list(settings.provider_order),
        )

    @property
    def available(self) -> list[str]:
        return [name for name in self._settings.provider_order if name in self._providers]

    @property
    def primary(self) -> ModelProvider | None:
        for name in self.available:
            return self._providers[name]
        return None

    def get(self, name: str) -> ModelProvider | None:
        return self._providers.get(name)

    async def complete(
        self, *, system: str, user: str, max_tokens: int, temperature: float = 0.7
    ) -> Completion:
        """Try each provider in order. Raises only when all of them fail.

        A rate-limited primary should degrade the answer's quality, not remove the
        feature — so a retryable failure moves to the next provider rather than
        surfacing.
        """
        if not self.available:
            raise ServiceUnavailableError(
                "No model provider is configured on this deployment.",
                code="ai_unavailable",
                details={"hint": "Set ANTHROPIC_API_KEY or OPENAI_API_KEY."},
            )

        last: Exception | None = None
        for name in self.available:
            provider = self._providers[name]
            try:
                return await provider.complete(
                    system=system, user=user, max_tokens=max_tokens, temperature=temperature
                )
            except ServiceUnavailableError as exc:
                # Retryable: rate limit or provider outage. Try the next one.
                logger.warning("ai.provider_failover", failed=name, error=str(exc))
                last = exc
                continue
            except UpstreamError as exc:
                # Deterministic. Another provider will reject it the same way, so
                # failing over would spend money to fail twice.
                raise exc from None

        raise last or ServiceUnavailableError("Every model provider failed.")

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
