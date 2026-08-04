"""Template management, deliverability, and the internal send API.

The `/internal/send` endpoint is how every other service asks for a message. It
takes a **template key**, never a body — letting callers pass raw content would put
copy in five services and make the unsubscribe footer something each of them has to
remember.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status
from sqlalchemy import case, func, select

from deps import DbSession, Dispatch, Inbox, PageOffset, Preferences, Templates
from knowledgeos_core import (
    ListResponse,
    MessageResponse,
    NotificationChannel,
    get_logger,
)
from knowledgeos_core.deps import InternalCaller, require_permission
from models import Delivery
from schemas import (
    DeliveryOut,
    DeliveryStats,
    SendRequest,
    SendResponse,
    SuppressionCreate,
    SuppressionOut,
    TemplateCreate,
    TemplateOut,
    TemplatePreview,
    TemplatePreviewRequest,
    TemplateUpdate,
)
from schemas import DeliveryStatus as Status
from services.templates import render_message
from settings import settings

logger = get_logger(__name__)

SETTINGS_WRITE = Depends(require_permission("settings:write"))
ANALYTICS_READ = Depends(require_permission("analytics:read"))

router = APIRouter(prefix="/v1/admin/notifications", tags=["admin"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])


# ---------------------------------------------------------------------------
# Templates
# ---------------------------------------------------------------------------


@router.get(
    "/templates",
    response_model=ListResponse[TemplateOut],
    summary="List templates",
    dependencies=[SETTINGS_WRITE],
)
async def list_templates(
    session: DbSession,
    templates: Templates,
    key: Annotated[str | None, Query(max_length=120)] = None,
) -> ListResponse[TemplateOut]:
    rows = await templates.list(session, key=key)
    return ListResponse[TemplateOut](
        items=[TemplateOut.model_validate(row) for row in rows], total=len(rows)
    )


@router.post(
    "/templates",
    response_model=TemplateOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a template",
    description=(
        "Bodies use `{{variable}}` substitution and nothing else — no expressions, "
        "no attribute access. Templates are editable through this API, so a "
        "template language with arbitrary evaluation would be remote code execution "
        "behind an admin token."
    ),
    dependencies=[SETTINGS_WRITE],
)
async def create_template(
    payload: TemplateCreate, session: DbSession, templates: Templates
) -> TemplateOut:
    return TemplateOut.model_validate(await templates.create(session, payload))


@router.patch(
    "/templates/{template_id}",
    response_model=TemplateOut,
    summary="Update a template",
    dependencies=[SETTINGS_WRITE],
)
async def update_template(
    template_id: uuid.UUID,
    payload: TemplateUpdate,
    session: DbSession,
    templates: Templates,
) -> TemplateOut:
    template = await templates.get(session, template_id)
    return TemplateOut.model_validate(await templates.update(session, template, payload))


@router.delete(
    "/templates/{template_id}",
    response_model=MessageResponse,
    summary="Deactivate a template",
    description="Deactivates rather than deletes — sent notifications reference the key.",
    dependencies=[SETTINGS_WRITE],
)
async def delete_template(
    template_id: uuid.UUID, session: DbSession, templates: Templates
) -> MessageResponse:
    await templates.delete(session, await templates.get(session, template_id))
    return MessageResponse(message="Template deactivated.")


@router.post(
    "/templates/{template_id}/preview",
    response_model=TemplatePreview,
    summary="Render a template without sending it",
    description=(
        "What every copy change should go through before it reaches a customer. "
        "Reports missing variables rather than rendering a raw `{{placeholder}}`."
    ),
    dependencies=[SETTINGS_WRITE],
)
async def preview_template(
    template_id: uuid.UUID,
    payload: TemplatePreviewRequest,
    session: DbSession,
    templates: Templates,
) -> TemplatePreview:
    template = await templates.get(session, template_id)
    rendered = render_message(template, payload.variables)
    return TemplatePreview(
        subject=rendered.subject,
        body_text=rendered.body_text,
        body_html=rendered.body_html,
        missing_variables=list(rendered.missing),
    )


# ---------------------------------------------------------------------------
# Deliverability
# ---------------------------------------------------------------------------


@router.get(
    "/deliveries",
    response_model=ListResponse[DeliveryOut],
    summary="Recent delivery attempts",
    description="Where 'the customer says they never got the email' is answered.",
    dependencies=[ANALYTICS_READ],
)
async def list_deliveries(
    session: DbSession,
    page: PageOffset,
    user_id: Annotated[uuid.UUID | None, Query()] = None,
    delivery_status: Annotated[Status | None, Query(alias="status")] = None,
    channel: Annotated[NotificationChannel | None, Query()] = None,
) -> ListResponse[DeliveryOut]:
    limit, offset = page
    conditions = []
    if user_id is not None:
        conditions.append(Delivery.user_id == user_id)
    if delivery_status is not None:
        conditions.append(Delivery.status == delivery_status)
    if channel is not None:
        conditions.append(Delivery.channel == channel)

    total = int(
        (await session.execute(select(func.count(Delivery.id)).where(*conditions))).scalar_one()
    )
    stmt = (
        select(Delivery)
        .where(*conditions)
        .order_by(Delivery.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    rows = list((await session.execute(stmt)).scalars().all())
    return ListResponse[DeliveryOut](
        items=[DeliveryOut.model_validate(row) for row in rows], total=total
    )


@router.get(
    "/stats",
    response_model=DeliveryStats,
    summary="Delivery statistics",
    description=(
        "`bounce_rate` is the number to watch. Above a few percent and the sending "
        "domain is in trouble — mailbox providers treat a high bounce rate as a "
        "signal that the sender is mailing a purchased list."
    ),
    dependencies=[ANALYTICS_READ],
)
async def delivery_stats(
    session: DbSession,
    days: Annotated[int, Query(ge=1, le=90)] = 7,
) -> DeliveryStats:
    since = datetime.now(UTC) - timedelta(days=days)
    window = [Delivery.created_at >= since]

    def _count(status_value: Status):  # type: ignore[no-untyped-def]
        # coalesce because SUM over an empty window is NULL, which would propagate
        # into every derived figure as None.
        return func.coalesce(func.sum(case((Delivery.status == status_value, 1), else_=0)), 0)

    row = (
        await session.execute(
            select(
                func.count(Delivery.id),
                _count(Status.SENT),
                _count(Status.DELIVERED),
                _count(Status.FAILED),
                _count(Status.BOUNCED),
                _count(Status.SKIPPED),
            ).where(*window)
        )
    ).one()
    total, sent, delivered, failed, bounced, skipped = (int(value) for value in row)

    by_channel = {
        str(channel): int(count)
        for channel, count in (
            await session.execute(
                select(Delivery.channel, func.count(Delivery.id))
                .where(*window)
                .group_by(Delivery.channel)
            )
        ).all()
    }

    # Of everything actually *attempted*: skipped messages were never sent, so
    # including them would understate the bounce rate exactly when a suppression
    # list is growing.
    attempted = total - skipped
    return DeliveryStats(
        window_days=days,
        total=total,
        sent=sent,
        delivered=delivered,
        failed=failed,
        bounced=bounced,
        skipped=skipped,
        bounce_rate=(bounced / attempted) if attempted else 0.0,
        by_channel=by_channel,
    )


@router.get(
    "/suppressions",
    response_model=ListResponse[SuppressionOut],
    summary="Suppressed addresses",
    dependencies=[ANALYTICS_READ],
)
async def list_suppressions(
    session: DbSession, preferences: Preferences, page: PageOffset
) -> ListResponse[SuppressionOut]:
    limit, offset = page
    rows = await preferences.list_suppressions(session, limit=limit, offset=offset)
    return ListResponse[SuppressionOut](
        items=[SuppressionOut.model_validate(row) for row in rows], total=len(rows)
    )


@router.post(
    "/suppressions",
    response_model=SuppressionOut,
    status_code=status.HTTP_201_CREATED,
    summary="Block an address",
    dependencies=[SETTINGS_WRITE],
)
async def create_suppression(
    payload: SuppressionCreate, session: DbSession, preferences: Preferences
) -> SuppressionOut:
    suppression = await preferences.suppress(
        session,
        channel=payload.channel,
        destination=payload.destination,
        reason=payload.reason,
        detail=payload.detail,
    )
    return SuppressionOut.model_validate(suppression)


@router.delete(
    "/suppressions",
    response_model=MessageResponse,
    summary="Unblock an address",
    description=(
        "An operator action, never automatic. Automatic removal would defeat the "
        "point — a bounce that resolves itself on retry is exactly the pattern that "
        "gets a sending domain blocklisted."
    ),
    dependencies=[SETTINGS_WRITE],
)
async def delete_suppression(
    session: DbSession,
    preferences: Preferences,
    destination: Annotated[str, Query(max_length=320)],
    channel: Annotated[NotificationChannel, Query()] = NotificationChannel.EMAIL,
) -> MessageResponse:
    lifted = await preferences.unsuppress(session, channel=channel, destination=destination)
    return MessageResponse(
        message="Suppression lifted." if lifted else "That address was not suppressed.",
        success=lifted,
    )


# ---------------------------------------------------------------------------
# Internal (HMAC-signed callers only)
# ---------------------------------------------------------------------------


@internal_router.post(
    "/send",
    response_model=SendResponse,
    summary="Send a notification (internal)",
    description=(
        "How every other service asks for a message. Takes a **template key**, "
        "never a body.\n\n"
        "The response lists every channel attempted — including the ones that were "
        "skipped and why. A caller that only sees successes cannot tell 'delivered' "
        "from 'silently dropped because the user opted out'."
    ),
)
async def internal_send(
    payload: SendRequest,
    caller: InternalCaller,
    session: DbSession,
    dispatcher: Dispatch,
) -> SendResponse:
    result = await dispatcher.send(session, payload)
    logger.info("notification.internal_send", caller=caller, template_key=payload.template_key)
    return SendResponse(
        notification_id=result.notification.id if result.notification else None,
        deliveries=[DeliveryOut.model_validate(delivery) for delivery in result.deliveries],
        skipped_reason=result.skipped_reason,
    )


@internal_router.post(
    "/maintenance/retry",
    response_model=MessageResponse,
    summary="Re-attempt failed deliveries (internal)",
    description=(
        "Driven by the worker. Retrying inside the request that failed would make a "
        "customer wait out an exponential backoff before their page loads."
    ),
)
async def retry_deliveries(
    caller: InternalCaller, session: DbSession, dispatcher: Dispatch
) -> MessageResponse:
    count = await dispatcher.retry_pending(session)
    return MessageResponse(message=f"Retried {count} deliveries.")


@internal_router.post(
    "/maintenance/prune",
    response_model=MessageResponse,
    summary="Prune old delivery records (internal)",
)
async def prune_deliveries(
    caller: InternalCaller,
    session: DbSession,
    days: Annotated[int, Query(ge=7, le=730)] = 90,
) -> MessageResponse:
    from sqlalchemy import delete as sql_delete

    cutoff = datetime.now(UTC) - timedelta(days=days)
    result = await session.execute(sql_delete(Delivery).where(Delivery.created_at < cutoff))
    await session.commit()
    return MessageResponse(message=f"Pruned {result.rowcount or 0} delivery records.")


@internal_router.post(
    "/maintenance/prune-inbox/{user_id}",
    response_model=MessageResponse,
    summary="Bound one user's in-app list (internal)",
)
async def prune_inbox(
    user_id: uuid.UUID,
    caller: InternalCaller,
    session: DbSession,
    inbox: Inbox,
) -> MessageResponse:
    pruned = await inbox.prune(session, user_id)
    return MessageResponse(message=f"Pruned {pruned} notifications.")


@internal_router.get(
    "/preferences/{user_id}",
    response_model=dict,
    summary="One user's effective preferences (internal)",
    description="Used by the admin service to explain why a message was not sent.",
)
async def internal_preferences(
    user_id: uuid.UUID,
    caller: InternalCaller,
    session: DbSession,
    preferences: Preferences,
) -> dict:
    rows = await preferences.list_for_user(session, user_id)
    return {
        "user_id": str(user_id),
        "preferences": [
            {"category": row.category, "channel": str(row.channel), "enabled": row.enabled}
            for row in rows
        ],
        "transactional_categories": list(settings.transactional_categories),
    }
