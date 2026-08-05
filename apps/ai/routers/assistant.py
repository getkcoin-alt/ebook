"""The user-facing AI surface: chat, recommendations, status.

Every route here costs money per call, so every one is rate-limited on the `ai`
policy — which is far tighter than the platform default — and every one runs the
per-user budget check before reaching a model.

`/status` exists so the UI can be honest. An AI feature that is off because the
budget is spent should say so, not fail silently or spin forever.
"""

from __future__ import annotations

import uuid

from fastapi import APIRouter, Depends, status

from deps import Budget, Chat, CurrentUser, DbSession, Generate, PageLimit, Providers, user_uuid
from knowledgeos_core import ListResponse, get_logger
from knowledgeos_core.deps import rate_limit
from schemas import (
    AiStatus,
    ChatMessage,
    ChatRequest,
    ChatResponse,
    ConversationDetail,
    ConversationOut,
    Recommendation,
    RecommendRequest,
    RecommendResponse,
)
from settings import settings

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/ai", tags=["ai"])

#: Model calls are slow and cost real money per request, so this is deliberately
#: much tighter than the platform default.
AI_LIMIT = Depends(rate_limit("ai"))


@router.get(
    "/status",
    response_model=AiStatus,
    summary="Whether AI features are usable right now",
    description=(
        "So the UI can be honest. A feature that is off because the daily budget is "
        "spent should say so, not fail silently or spin forever."
    ),
)
async def ai_status(
    session: DbSession, providers: Providers, budget: Budget, principal: CurrentUser
) -> AiStatus:
    state = await budget.state(session, user_id=user_uuid(principal))
    primary = providers.primary
    return AiStatus(
        available=bool(providers.available) and not state.exhausted,
        providers=providers.available,
        model=primary.model if primary else None,
        budget_remaining_usd=round(state.remaining_usd, 4),
        cache_enabled=settings.cache_enabled,
        moderation_enabled=settings.moderation_enabled,
    )


# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------


@router.post(
    "/chat",
    response_model=ChatResponse,
    summary="Ask the assistant",
    description=(
        "Answers are grounded in real catalogue results — the question searches the "
        "catalogue first, and the model is told it may only name books from those "
        "results. A recommendation the store cannot sell is worse than none.\n\n"
        "Passing `book_id` scopes the conversation to one book, and **requires that "
        "book to be in your library**. Otherwise this would be a way to read a book "
        "you have not bought, one question at a time."
    ),
    dependencies=[AI_LIMIT],
)
async def chat(
    payload: ChatRequest,
    principal: CurrentUser,
    session: DbSession,
    chat_service: Chat,
) -> ChatResponse:
    conversation, reply, sources = await chat_service.ask(
        session,
        user_id=user_uuid(principal),
        message=payload.message,
        conversation_id=payload.conversation_id,
        book_id=payload.book_id,
    )
    return ChatResponse(
        conversation_id=conversation.id,
        message=ChatMessage(role=reply.role, content=reply.content, created_at=reply.created_at),
        sources=sources,
        input_tokens=reply.input_tokens,
        output_tokens=reply.output_tokens,
    )


@router.get(
    "/conversations",
    response_model=ListResponse[ConversationOut],
    summary="Your conversations",
)
async def list_conversations(
    principal: CurrentUser,
    session: DbSession,
    chat_service: Chat,
    limit: PageLimit,
) -> ListResponse[ConversationOut]:
    rows = await chat_service.list_for_user(session, user_uuid(principal), limit=limit)
    return ListResponse[ConversationOut](
        items=[ConversationOut.model_validate(row) for row in rows], total=len(rows)
    )


@router.get(
    "/conversations/{conversation_id}",
    response_model=ConversationDetail,
    summary="One conversation with its messages",
)
async def get_conversation(
    conversation_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
    chat_service: Chat,
) -> ConversationDetail:
    conversation = await chat_service.get_for_user(session, conversation_id, user_uuid(principal))
    messages = await chat_service.messages(session, conversation.id)
    return ConversationDetail(
        id=conversation.id,
        title=conversation.title,
        book_id=conversation.book_id,
        message_count=conversation.message_count,
        created_at=conversation.created_at,
        updated_at=conversation.updated_at,
        messages=[
            ChatMessage(role=m.role, content=m.content, created_at=m.created_at) for m in messages
        ],
    )


@router.delete(
    "/conversations/{conversation_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a conversation",
    description="Hard delete — a conversation is the user's, and they may remove it.",
)
async def delete_conversation(
    conversation_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
    chat_service: Chat,
):  # type: ignore[no-untyped-def]
    from fastapi import Response

    conversation = await chat_service.get_for_user(session, conversation_id, user_uuid(principal))
    await chat_service.delete(session, conversation)
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Recommendations
# ---------------------------------------------------------------------------


@router.post(
    "/recommend",
    response_model=RecommendResponse,
    summary="Personalised recommendations",
    description=(
        "Returns `fallback: true` when no model was available and the list came from "
        "catalogue popularity instead. The UI must not claim an AI recommendation it "
        "did not get."
    ),
    dependencies=[AI_LIMIT],
)
async def recommend(
    payload: RecommendRequest,
    principal: CurrentUser,
    session: DbSession,
    chat_service: Chat,
    providers: Providers,
    generator: Generate,
) -> RecommendResponse:
    from schemas import BookContext, TaskKind

    query = payload.prompt or "books this reader would enjoy next"
    catalogue = await chat_service._catalogue_context(query, limit=20)

    if not providers.available or not catalogue:
        # Honest degradation: the search results in catalogue order, flagged as a
        # fallback so the UI does not present popularity as a personal suggestion.
        logger.info("ai.recommend_fallback", has_providers=bool(providers.available))
        return RecommendResponse(
            recommendations=[
                Recommendation(
                    book_id=uuid.UUID(book["id"]) if book.get("id") else None,
                    title=str(book.get("title", "")),
                    reason="Popular in the catalogue right now.",
                )
                for book in catalogue[: payload.limit]
            ],
            fallback=True,
        )

    listing = "\n".join(f"- {book.get('title')} ({book.get('id')})" for book in catalogue)
    result = await generator.generate(
        session,
        kind=TaskKind.RECOMMEND,
        context=BookContext(title="[recommendations]", excerpt=query),
        user_id=user_uuid(principal),
        source="recommend",
        force_refresh=True,
        extra_prompt=f"Catalogue:\n{listing}\n\nRecommend at most {payload.limit}.",
    )

    by_title = {str(book.get("title", "")).lower(): book for book in catalogue}
    recommendations: list[Recommendation] = []
    for item in (result.data or {}).get("recommendations", [])[: payload.limit]:
        title = str(item.get("title", "")).strip()
        # Only titles that exist in the catalogue survive. The prompt forbids
        # inventing one, but a prompt is not an enforcement mechanism — this is.
        match = by_title.get(title.lower())
        if match is None:
            logger.info("ai.recommend_hallucination_dropped", title=title)
            continue
        recommendations.append(
            Recommendation(
                book_id=uuid.UUID(match["id"]) if match.get("id") else None,
                title=title,
                reason=str(item.get("reason", "")).strip()[:300],
            )
        )

    return RecommendResponse(recommendations=recommendations, fallback=False)
