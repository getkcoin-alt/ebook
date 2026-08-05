"""Conversational assistant, grounded in the catalogue.

Two constraints shape this, and both come from the same problem: a language model
will confidently invent a book that does not exist, and a reader will go looking for
it.

**Answers are grounded in real catalogue results.** The user's question is used to
search the catalogue first; those results become the context the model answers from,
and the prompt tells it not to name a book outside them. A recommendation the store
cannot sell is worse than no recommendation.

**Book access is checked server-side.** When a conversation is grounded in a specific
book, entitlement is verified before any of its text reaches a prompt. Otherwise "ask
about this book" is a way to read a book you have not bought, one question at a time.

History is trimmed rather than summarised. Summarising costs a model call per turn to
save tokens on the next one, which is usually a losing trade at these context sizes.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import (
    ForbiddenError,
    NotFoundError,
    ServiceUnavailableError,
    get_logger,
)
from knowledgeos_core.http import ServiceRegistry
from models import Conversation, Message
from schemas import BookContext, MessageRole, TaskKind
from services.budget import BudgetService
from services.generator import Generator
from services.providers import ProviderRegistry
from settings import Settings

logger = get_logger(__name__)

#: Turns kept as context. Beyond this the earliest are dropped — a conversation long
#: enough to hit it has usually moved on from where it started anyway.
HISTORY_TURNS = 12


class ChatService:
    def __init__(
        self,
        settings: Settings,
        generator: Generator,
        providers: ProviderRegistry,
        budget: BudgetService,
        services: ServiceRegistry | None = None,
    ) -> None:
        self._settings = settings
        self._generator = generator
        self._providers = providers
        self._budget = budget
        self._services = services

    # ---- conversations --------------------------------------------------

    async def get_for_user(
        self, session: AsyncSession, conversation_id: uuid.UUID, user_id: uuid.UUID
    ) -> Conversation:
        conversation = await session.get(Conversation, conversation_id)
        # 404 rather than 403 for someone else's: a 403 confirms it exists.
        if conversation is None or conversation.user_id != user_id:
            raise NotFoundError("Conversation not found.")
        return conversation

    async def list_for_user(
        self, session: AsyncSession, user_id: uuid.UUID, *, limit: int = 30
    ) -> list[Conversation]:
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .limit(limit)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def messages(self, session: AsyncSession, conversation_id: uuid.UUID) -> list[Message]:
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at)
        )
        return list((await session.execute(stmt)).scalars().all())

    async def delete(self, session: AsyncSession, conversation: Conversation) -> None:
        await session.delete(conversation)
        await session.commit()

    # ---- grounding ------------------------------------------------------

    async def _catalogue_context(self, question: str, limit: int = 6) -> list[dict]:
        """Search the catalogue for books relevant to the question.

        This is what makes an answer groundable. Without it the model answers from
        training data and names books this store does not carry.
        """
        if self._services is None:
            return []
        try:
            payload = await self._services.get("search").get_json(
                "/v1/search", params={"q": question[:200], "limit": limit, "facets": "false"}
            )
        except Exception as exc:
            # A search outage degrades the answer; it does not remove the feature.
            logger.warning("ai.chat_grounding_failed", error=str(exc))
            return []

        return [
            {
                "id": hit.get("id"),
                "title": hit.get("title"),
                "slug": hit.get("slug"),
                "authors": [a.get("name") for a in (hit.get("authors") or [])],
                "description": (hit.get("description") or "")[:400],
            }
            for hit in (payload or {}).get("hits", [])
        ]

    async def _require_book_access(self, book_id: uuid.UUID, user_id: uuid.UUID) -> dict:
        """Verify the reader owns the book before any of it enters a prompt.

        Without this, "ask about this book" is a way to read a book you have not
        bought, one question at a time.
        """
        if self._services is None:
            raise ServiceUnavailableError("The catalogue is unavailable.")
        try:
            access = await self._services.get("books").get_json(f"/internal/books/{book_id}")
        except Exception as exc:
            raise ServiceUnavailableError("The catalogue is unavailable.") from exc

        if not access:
            raise NotFoundError("Book not found.")
        # The books service is authoritative on entitlements; this service only asks.
        try:
            entitlement = await self._services.get("books").post_json(
                "/internal/entitlements/check",
                {"user_id": str(user_id), "book_ids": [str(book_id)]},
            )
        except Exception as exc:
            raise ServiceUnavailableError("Could not verify your access.") from exc

        if str(book_id) not in {str(b) for b in (entitlement or {}).get("owned_book_ids", [])}:
            raise ForbiddenError(
                "You need this book in your library to ask about it.",
                details={"book_id": str(book_id)},
            )
        return access

    # ---- the turn -------------------------------------------------------

    async def ask(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        message: str,
        conversation_id: uuid.UUID | None = None,
        book_id: uuid.UUID | None = None,
    ) -> tuple[Conversation, Message, list[dict]]:
        """One turn. Returns the conversation, the reply, and cited sources."""
        await self._budget.check(session, user_id=user_id)

        if conversation_id is not None:
            conversation = await self.get_for_user(session, conversation_id, user_id)
        else:
            conversation = Conversation(
                user_id=user_id,
                book_id=book_id,
                # The first message becomes the title, so the sidebar is readable
                # without asking the user to name anything.
                title=message.strip()[:80],
            )
            session.add(conversation)
            await session.flush()

        grounding: list[dict] = []
        book_note = ""
        if conversation.book_id is not None:
            book = await self._require_book_access(conversation.book_id, user_id)
            book_note = (
                f"The reader is asking about this specific book:\n"
                f"Title: {book.get('title')}\n"
                f"Authors: {', '.join(a.get('name', '') for a in (book.get('authors') or []))}\n"
                f"Description: {(book.get('description') or '')[:2000]}"
            )
            grounding = [
                {"id": book.get("id"), "title": book.get("title"), "slug": book.get("slug")}
            ]
        else:
            grounding = await self._catalogue_context(message)

        history = await self._history_block(session, conversation.id)
        catalogue_block = _render_catalogue(grounding) if grounding else ""

        extra = "\n\n".join(part for part in (book_note, catalogue_block, history) if part)

        session.add(
            Message(
                conversation_id=conversation.id,
                role=MessageRole.USER,
                content=message.strip(),
                created_at=datetime.now(UTC),
            )
        )
        await session.flush()

        result = await self._generator.generate(
            session,
            kind=TaskKind.CHAT,
            # The question goes in `excerpt` — the user turn. Never the system prompt.
            context=BookContext(title="[chat]", excerpt=message.strip()),
            user_id=user_id,
            source="chat",
            # Chat is conversational: a cached answer to "tell me more" would be the
            # previous conversation's answer.
            force_refresh=True,
            extra_prompt=extra,
        )

        reply = Message(
            conversation_id=conversation.id,
            role=MessageRole.ASSISTANT,
            content=(result.content or "").strip() or "I could not answer that.",
            sources=grounding,
            input_tokens=result.input_tokens,
            output_tokens=result.output_tokens,
            created_at=datetime.now(UTC),
        )
        session.add(reply)

        conversation.message_count += 2
        conversation.updated_at = datetime.now(UTC)
        await session.commit()

        logger.info(
            "ai.chat_turn",
            conversation_id=str(conversation.id),
            grounded=bool(grounding),
            book_scoped=conversation.book_id is not None,
        )
        return conversation, reply, grounding

    async def _history_block(self, session: AsyncSession, conversation_id: uuid.UUID) -> str:
        """Recent turns, oldest first.

        Trimmed rather than summarised: summarising costs a model call per turn to
        save tokens on the next one, which is a losing trade at these context sizes.
        """
        stmt = (
            select(Message)
            .where(Message.conversation_id == conversation_id)
            .order_by(Message.created_at.desc())
            .limit(HISTORY_TURNS)
        )
        rows = list((await session.execute(stmt)).scalars().all())
        if not rows:
            return ""
        rows.reverse()
        lines = [f"{row.role}: {row.content[:1500]}" for row in rows]
        return "Conversation so far:\n" + "\n".join(lines)

    async def prune(self, session: AsyncSession, *, user_id: uuid.UUID) -> int:
        """Keep a user's conversation list bounded, oldest first."""
        keep = self._settings.max_conversations_per_user
        stmt = (
            select(Conversation)
            .where(Conversation.user_id == user_id)
            .order_by(Conversation.updated_at.desc())
            .offset(keep)
        )
        stale = list((await session.execute(stmt)).scalars().all())
        for conversation in stale:
            await session.delete(conversation)
        if stale:
            await session.commit()
        return len(stale)

    async def count_for_user(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        stmt = select(func.count(Conversation.id)).where(Conversation.user_id == user_id)
        return int((await session.execute(stmt)).scalar_one())


def _render_catalogue(books: list[dict]) -> str:
    """The books the model is allowed to name, as a numbered list."""
    lines = ["Books available in this store (you may only name books from this list):"]
    for index, book in enumerate(books, start=1):
        authors = ", ".join(filter(None, book.get("authors") or []))
        lines.append(
            f"{index}. {book.get('title')}"
            + (f" — {authors}" if authors else "")
            + (f"\n   {book.get('description')}" if book.get("description") else "")
        )
    return "\n".join(lines)
