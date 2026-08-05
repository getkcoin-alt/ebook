"""Content moderation.

Runs before user text reaches a model, and before a review reaches the catalogue.

**It fails closed.** When the classifier is unavailable, submission is refused rather
than waved through. That is the uncomfortable choice and it is the right one: an
unmoderated path opens exactly when the moderation service is under strain, which is
exactly when someone is most likely to be abusing it. A user who has to retry in a
minute is a smaller problem than content nobody checked.

One judgement is encoded in the prompt and worth stating plainly: **a review is
allowed to be harshly negative about a book.** Criticism of a work is not harassment
of its author, and a moderator that cannot tell the difference silently becomes a
tool for suppressing bad reviews — which is worse for a bookstore than the occasional
rude sentence.
"""

from __future__ import annotations

import uuid

from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import ServiceUnavailableError, get_logger
from schemas import BookContext, ModerationResult, TaskKind
from services.generator import Generator
from settings import Settings

logger = get_logger(__name__)

#: Matched before a model is called. These are the cases where a classifier adds
#: nothing — an empty string is not abusive, and a wall of one character is spam by
#: construction.
_MAX_REPEAT_RUN = 60


class ModerationService:
    def __init__(self, settings: Settings, generator: Generator) -> None:
        self._settings = settings
        self._generator = generator

    @property
    def enabled(self) -> bool:
        return self._settings.moderation_enabled

    async def check(
        self,
        session: AsyncSession,
        *,
        content: str,
        context: str = "review",
        user_id: uuid.UUID | None = None,
    ) -> ModerationResult:
        if not self.enabled:
            return ModerationResult(allowed=True)

        text = content.strip()
        if not text:
            return ModerationResult(allowed=False, flags=["empty"], reason="There is nothing here.")

        # Cheap structural checks first. A model call to decide that "aaaa…" is spam
        # is a model call spent badly.
        if _longest_run(text) >= _MAX_REPEAT_RUN:
            return ModerationResult(
                allowed=False,
                flags=["spam"],
                score=1.0,
                reason="That looks like repeated characters rather than a comment.",
            )

        try:
            result = await self._generator.generate(
                session,
                kind=TaskKind.MODERATION,
                # Reuses the book-context shape so moderation flows through exactly
                # the same cache, budget and ledger path as everything else. The
                # submitted text goes in `excerpt` — the user turn — never in the
                # system prompt.
                context=BookContext(title=f"[{context}]", excerpt=text),
                user_id=user_id,
                source="moderation",
            )
        except ServiceUnavailableError:
            # Fails closed. An unmoderated path opens exactly when moderation is
            # under strain, which is when it is most needed.
            if self._settings.moderation_fail_closed:
                logger.warning("ai.moderation_unavailable_refusing", context=context)
                raise
            logger.warning("ai.moderation_unavailable_allowing", context=context)
            return ModerationResult(allowed=True)

        data = result.data or {}
        allowed = bool(data.get("allowed", True))
        flags = [str(flag) for flag in (data.get("flags") or [])][:10]
        # A model that flags something but forgets to set `allowed: false` should
        # still block. Trusting the boolean alone makes the outcome depend on the
        # model remembering to set two fields consistently.
        if flags:
            allowed = False

        return ModerationResult(
            allowed=allowed,
            flags=flags,
            score=float(data.get("score") or (0.0 if allowed else 1.0)),
            reason=(data.get("reason") or None) if not allowed else None,
        )


def _longest_run(text: str) -> int:
    from itertools import pairwise

    longest = run = 1
    for previous, current in pairwise(text):
        run = run + 1 if current == previous else 1
        longest = max(longest, run)
    return longest
