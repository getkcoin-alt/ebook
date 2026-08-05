"""The generation path — cache, budget, moderation, model, ledger.

The order of operations is the design, and each step is there to avoid paying for
something:

1. **Cache first.** Book copy is generated once and read thousands of times.
   Regenerating per request is pure waste, and non-determinism means the description
   changes each time for no reason a reader would understand.
2. **Budget second.** Checked *before* the model call, never after. A ceiling
   enforced afterwards has already spent the money it was meant to prevent.
3. **Moderate third**, for user-supplied text. Cheap classifier before expensive
   generation.
4. **Then the model**, then record the ledger row and the spend in one transaction.

**Every outcome is recorded** — cached, blocked and failed included. A ledger of only
successful calls makes the cache look free, moderation look like it never ran, and
the failure rate invisible.
"""

from __future__ import annotations

import hashlib
import time
import uuid
from dataclasses import dataclass

import orjson
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import BadRequestError, ServiceUnavailableError, get_logger
from knowledgeos_core.redis import RedisClient
from models import CachedResult, Generation
from schemas import (
    BookContext,
    GenerationStatus,
    TaskKind,
)
from services import prompts
from services.budget import BudgetService
from services.providers import ProviderRegistry
from settings import Settings

logger = get_logger(__name__)


@dataclass(slots=True)
class GenerationResult:
    """What the caller gets back, whatever path produced it."""

    id: uuid.UUID
    kind: TaskKind
    status: GenerationStatus
    content: str | None = None
    data: dict | None = None
    provider: str | None = None
    model: str | None = None
    input_tokens: int = 0
    output_tokens: int = 0
    cost_usd: float = 0.0
    cached: bool = False
    error: str | None = None


def cache_key(kind: TaskKind, context: BookContext, locale: str) -> str:
    """A stable fingerprint of everything that affects the output.

    Sorted keys so field ordering does not change the hash, and the excerpt is
    included — a book whose text was re-extracted should regenerate its copy, because
    the old copy was written from different material.
    """
    payload = {
        "kind": str(kind),
        "locale": locale,
        "title": context.title,
        "subtitle": context.subtitle,
        "authors": sorted(context.authors),
        "categories": sorted(context.categories),
        "language": context.language,
        "excerpt": (context.excerpt or "")[:8_000],
        "existing": (context.existing_description or "")[:2_000],
    }
    return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()


def parse_json_output(text: str) -> dict:
    """Pull a JSON object out of a model response.

    Models wrap JSON in prose or a code fence no matter how firmly the prompt says
    not to. Failing the whole generation over a stray ```json would waste a call that
    already succeeded and already cost money, so this recovers what it can.
    """
    cleaned = text.strip()
    if cleaned.startswith("```"):
        cleaned = cleaned.split("\n", 1)[-1]
        if cleaned.endswith("```"):
            cleaned = cleaned[:-3]
        cleaned = cleaned.strip()

    try:
        parsed = orjson.loads(cleaned)
        if isinstance(parsed, dict):
            return parsed
    except orjson.JSONDecodeError:
        pass

    # Last resort: the outermost braces.
    start, end = cleaned.find("{"), cleaned.rfind("}")
    if start != -1 and end > start:
        try:
            parsed = orjson.loads(cleaned[start : end + 1])
            if isinstance(parsed, dict):
                return parsed
        except orjson.JSONDecodeError:
            pass

    logger.warning("ai.json_parse_failed", preview=cleaned[:200])
    return {}


class Generator:
    def __init__(
        self,
        settings: Settings,
        providers: ProviderRegistry,
        budget: BudgetService,
        redis: RedisClient | None = None,
    ) -> None:
        self._settings = settings
        self._providers = providers
        self._budget = budget
        self._redis = redis

    # ---- cache ----------------------------------------------------------

    async def _from_cache(self, session: AsyncSession, key: str) -> CachedResult | None:
        if not self._settings.cache_enabled:
            return None

        if self._redis is not None:
            try:
                hit = await self._redis.get_json(f"gen:{key}")
                if hit:
                    return CachedResult(
                        cache_key=key,
                        kind=TaskKind(hit["kind"]),
                        content=hit.get("content"),
                        data=hit.get("data") or {},
                        model=hit.get("model"),
                    )
            except Exception as exc:
                # A cache outage must not stop generation; it just costs money.
                logger.warning("ai.cache_read_failed", error=str(exc))

        # Redis is allowed to be empty — it is a cache. This table is what survives a
        # flush, and it matters because regenerating every description on the
        # platform after an eviction is a bill, not an inconvenience.
        stmt = select(CachedResult).where(CachedResult.cache_key == key)
        row = (await session.execute(stmt)).scalars().one_or_none()
        if row is not None:
            row.hits += 1
            await self._warm(key, row)
        return row

    async def _warm(self, key: str, row: CachedResult) -> None:
        if self._redis is None:
            return
        try:
            await self._redis.set_json(
                f"gen:{key}",
                {
                    "kind": str(row.kind),
                    "content": row.content,
                    "data": row.data,
                    "model": row.model,
                },
                ttl=self._settings.cache_ttl,
            )
        except Exception as exc:
            logger.warning("ai.cache_write_failed", error=str(exc))

    async def _store(
        self,
        session: AsyncSession,
        *,
        key: str,
        kind: TaskKind,
        book_id: uuid.UUID | None,
        content: str | None,
        data: dict,
        model: str | None,
    ) -> None:
        row = CachedResult(
            cache_key=key, kind=kind, book_id=book_id, content=content, data=data, model=model
        )
        # A SAVEPOINT: two concurrent identical requests both miss the cache and both
        # try to write it. Losing that race is fine — it is the same content — but it
        # must not roll back the generation row alongside it.
        savepoint = await session.begin_nested()
        session.add(row)
        try:
            await session.flush()
            await savepoint.commit()
        except IntegrityError:
            await savepoint.rollback()
            return
        await self._warm(key, row)

    # ---- generation -----------------------------------------------------

    async def generate(
        self,
        session: AsyncSession,
        *,
        kind: TaskKind,
        context: BookContext,
        book_id: uuid.UUID | None = None,
        user_id: uuid.UUID | None = None,
        source: str | None = None,
        locale: str = "en",
        force_refresh: bool = False,
        extra_prompt: str = "",
    ) -> GenerationResult:
        """Run one task end to end. Commits its own transaction."""
        key = cache_key(kind, context, locale)

        if not force_refresh:
            cached = await self._from_cache(session, key)
            if cached is not None:
                record = Generation(
                    kind=kind,
                    status=GenerationStatus.CACHED,
                    user_id=user_id,
                    book_id=book_id,
                    source=source,
                    model=cached.model,
                    content=cached.content,
                    data=cached.data or {},
                    cache_key=key,
                )
                session.add(record)
                await session.commit()
                logger.info(
                    "ai.cache_hit", kind=str(kind), book_id=str(book_id) if book_id else None
                )
                return GenerationResult(
                    id=record.id,
                    kind=kind,
                    status=GenerationStatus.CACHED,
                    content=cached.content,
                    data=cached.data or {},
                    model=cached.model,
                    cached=True,
                )

        # Before the call, never after. A ceiling enforced afterwards has already
        # spent the money it exists to prevent.
        await self._budget.check(session, user_id=user_id)

        if not self._providers.available:
            raise ServiceUnavailableError(
                "No model provider is configured on this deployment.", code="ai_unavailable"
            )

        system, user_prompt = prompts.build(
            kind, context, excerpt_limit=self._settings.max_input_chars, extra=extra_prompt
        )
        if len(user_prompt) > self._settings.max_input_chars:
            raise BadRequestError(
                "That input is too large to process.",
                code="input_too_large",
                details={"max_chars": self._settings.max_input_chars},
            )

        started = time.perf_counter()
        try:
            completion = await self._providers.complete(
                system=system,
                user=user_prompt,
                max_tokens=self._settings.max_output_tokens,
                # Structured output gets a low temperature: JSON that varies run to
                # run is JSON that sometimes fails to parse.
                temperature=0.2 if kind in _STRUCTURED else 0.7,
            )
        except Exception as exc:
            # Failures are recorded too. A ledger of only successes hides the failure
            # rate, which is the number that tells you a provider is degrading.
            session.add(
                Generation(
                    kind=kind,
                    status=GenerationStatus.FAILED,
                    user_id=user_id,
                    book_id=book_id,
                    source=source,
                    prompt=user_prompt[:8_000],
                    cache_key=key,
                    error=str(exc)[:2000],
                    latency_ms=int((time.perf_counter() - started) * 1000),
                )
            )
            await session.commit()
            raise

        latency_ms = int((time.perf_counter() - started) * 1000)
        if completion.usage_missing:
            # Recorded with zero tokens and flagged. A made-up number in a cost
            # ledger is worse than a missing one.
            logger.warning("ai.usage_missing", provider=completion.provider, model=completion.model)

        data = parse_json_output(completion.content) if kind in _STRUCTURED else {}
        content = None if kind in _STRUCTURED else completion.content.strip()

        record = Generation(
            kind=kind,
            status=GenerationStatus.SUCCEEDED,
            user_id=user_id,
            book_id=book_id,
            source=source,
            provider=completion.provider,
            model=completion.model,
            prompt=user_prompt[:8_000],
            content=content,
            data=data,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost_usd=completion.cost_usd,
            latency_ms=latency_ms,
            cache_key=key,
        )
        session.add(record)

        await self._budget.record(
            session,
            cost_usd=completion.cost_usd,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            user_id=user_id,
        )
        await self._store(
            session,
            key=key,
            kind=kind,
            book_id=book_id,
            content=content,
            data=data,
            model=completion.model,
        )
        await session.commit()

        logger.info(
            "ai.generated",
            kind=str(kind),
            provider=completion.provider,
            model=completion.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost_usd=completion.cost_usd,
            latency_ms=latency_ms,
        )
        return GenerationResult(
            id=record.id,
            kind=kind,
            status=GenerationStatus.SUCCEEDED,
            content=content,
            data=data,
            provider=completion.provider,
            model=completion.model,
            input_tokens=completion.input_tokens,
            output_tokens=completion.output_tokens,
            cost_usd=completion.cost_usd,
        )


#: Tasks whose output is JSON rather than prose.
_STRUCTURED = {
    TaskKind.SEO,
    TaskKind.TAGS,
    TaskKind.CATEGORIES,
    TaskKind.RECOMMEND,
    TaskKind.MODERATION,
}
