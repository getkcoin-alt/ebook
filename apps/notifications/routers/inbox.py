"""The user-facing surface: the notification bell, preferences, and unsubscribe.

Everything except unsubscribe requires a token, and the user id always comes from
that token. Unsubscribe is the exception — it is reached from an email footer by
someone who may not be signed in, which is exactly why its token is signed.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from deps import CursorLimit, DbSession, Inbox, Preferences, user_uuid
from knowledgeos_core import BadRequestError, MessageResponse, NotificationChannel, get_logger
from knowledgeos_core.deps import CurrentUser, rate_limit
from models import DeviceToken
from schemas import (
    ChannelStatus,
    DeviceTokenOut,
    DeviceTokenRegister,
    MarkReadRequest,
    NotificationOut,
    NotificationPage,
    PreferenceOut,
    PreferencesResponse,
    PreferencesUpdateRequest,
    UnreadCount,
    UnsubscribeRequest,
)
from services import KNOWN_CATEGORIES, verify_unsubscribe_token
from settings import settings

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/notifications", tags=["notifications"])


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


@router.get(
    "",
    response_model=NotificationPage,
    summary="Your notifications",
    description=(
        "Keyset paginated. This is a list that grows *while it is being read*, so "
        "offset paging would shift every boundary as new notifications arrive."
    ),
)
async def list_notifications(
    principal: CurrentUser,
    session: DbSession,
    inbox: Inbox,
    limit: CursorLimit,
    cursor: Annotated[str | None, Query()] = None,
    unread_only: Annotated[bool, Query()] = False,
    include_archived: Annotated[bool, Query()] = False,
) -> NotificationPage:
    user_id = user_uuid(principal)
    rows, next_cursor, has_more = await inbox.list(
        session,
        user_id=user_id,
        cursor=cursor,
        limit=limit,
        unread_only=unread_only,
        include_archived=include_archived,
    )
    return NotificationPage(
        items=[NotificationOut.model_validate(row) for row in rows],
        next_cursor=next_cursor,
        has_more=has_more,
        unread_count=await inbox.unread_count(session, user_id),
    )


@router.get(
    "/unread-count",
    response_model=UnreadCount,
    summary="Unread badge count",
    description=(
        "Its own endpoint because the bell polls this far more often than it "
        "fetches the list — making the badge load the full list would be the most "
        "expensive query on the platform, several times a minute per signed-in user."
    ),
    dependencies=[Depends(rate_limit("authenticated"))],
)
async def unread_count(principal: CurrentUser, session: DbSession, inbox: Inbox) -> UnreadCount:
    return UnreadCount(unread=await inbox.unread_count(session, user_uuid(principal)))


@router.post(
    "/read",
    response_model=MessageResponse,
    summary="Mark notifications read",
    description="Omit `notification_ids` to mark everything read.",
)
async def mark_read(
    payload: MarkReadRequest,
    principal: CurrentUser,
    session: DbSession,
    inbox: Inbox,
) -> MessageResponse:
    changed = await inbox.mark_read(
        session, user_id=user_uuid(principal), notification_ids=payload.notification_ids
    )
    return MessageResponse(message=f"Marked {changed} notifications read.")


@router.post(
    "/{notification_id}/archive",
    response_model=NotificationOut,
    summary="Dismiss a notification",
    description="Hidden from the list, not deleted — the delivery records reference it.",
)
async def archive(
    notification_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
    inbox: Inbox,
) -> NotificationOut:
    notification = await inbox.get_for_user(session, notification_id, user_uuid(principal))
    return NotificationOut.model_validate(await inbox.archive(session, notification))


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


@router.get(
    "/preferences",
    response_model=PreferencesResponse,
    summary="Your notification preferences",
    description=(
        "Transactional categories come back with `locked: true`. The UI should "
        "render those toggles disabled rather than hiding them — a user is entitled "
        "to see that the message exists and that it is not marketing."
    ),
)
async def get_preferences(
    principal: CurrentUser, session: DbSession, preferences: Preferences
) -> PreferencesResponse:
    user_id = user_uuid(principal)
    stored = {
        (row.category, row.channel): row
        for row in await preferences.list_for_user(session, user_id)
    }

    items: list[PreferenceOut] = []
    for category in sorted(KNOWN_CATEGORIES):
        locked = preferences.is_transactional(category)
        for channel in (NotificationChannel.IN_APP, NotificationChannel.EMAIL):
            row = stored.get((category, channel))
            items.append(
                PreferenceOut(
                    category=category,
                    channel=channel,
                    # Absence means the default, which is on. There is no row per
                    # user per category at signup: writing millions of rows that all
                    # say "yes" makes the table useless.
                    enabled=True if row is None else row.enabled,
                    locked=locked,
                )
            )

    return PreferencesResponse(
        user_id=user_id,
        preferences=items,
        transactional_categories=list(settings.transactional_categories),
    )


@router.put(
    "/preferences",
    response_model=MessageResponse,
    summary="Update your preferences",
    description=(
        "Attempts to disable a transactional category are accepted and ignored "
        "rather than rejected — the UI already shows those locked, and a 400 for a "
        "state the user cannot reach is noise."
    ),
)
async def update_preferences(
    payload: PreferencesUpdateRequest,
    principal: CurrentUser,
    session: DbSession,
    preferences: Preferences,
) -> MessageResponse:
    updated = await preferences.set(
        session, user_id=user_uuid(principal), updates=payload.preferences
    )
    return MessageResponse(message=f"Updated {len(updated)} preferences.")


@router.post(
    "/unsubscribe",
    response_model=MessageResponse,
    summary="One-click unsubscribe",
    description=(
        "Reached from an email footer by someone who may not be signed in, so the "
        "token is a signed blob rather than a user id — a URL containing an id "
        "would let anyone unsubscribe anyone by editing it.\n\n"
        "Omit `category` to opt out of every non-transactional email."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def unsubscribe(
    payload: UnsubscribeRequest,
    session: DbSession,
    preferences: Preferences,
) -> MessageResponse:
    user_id = verify_unsubscribe_token(settings.unsubscribe_secret, payload.token)
    if user_id is None:
        raise BadRequestError(
            "That unsubscribe link is not valid.", code="invalid_unsubscribe_token"
        )

    if payload.category:
        if preferences.is_transactional(payload.category):
            # Nothing to do, and saying so plainly is better than a silent success
            # that leaves the user expecting the mail to stop.
            return MessageResponse(
                message=(
                    "That category covers essential account and order messages, "
                    "which cannot be turned off."
                ),
                success=False,
            )
        from schemas import PreferenceUpdate

        await preferences.set(
            session,
            user_id=user_id,
            updates=[
                PreferenceUpdate(
                    category=payload.category, channel=NotificationChannel.EMAIL, enabled=False
                )
            ],
        )
        return MessageResponse(message="You have been unsubscribed from those emails.")

    count = await preferences.disable_all(
        session, user_id=user_id, channel=NotificationChannel.EMAIL
    )
    return MessageResponse(
        message=(
            f"You have been unsubscribed from {count} email categories. "
            "Essential account and order messages will still be sent."
        )
    )


# ---------------------------------------------------------------------------
# Devices and channels
# ---------------------------------------------------------------------------


@router.post(
    "/devices",
    response_model=DeviceTokenOut,
    status_code=status.HTTP_201_CREATED,
    summary="Register a push token",
    description="Idempotent on the token. Tokens rotate, so they key the row, not the device.",
)
async def register_device(
    payload: DeviceTokenRegister,
    principal: CurrentUser,
    session: DbSession,
) -> DeviceTokenOut:
    from datetime import UTC, datetime

    from sqlalchemy import select

    user_id = user_uuid(principal)
    existing = (
        (await session.execute(select(DeviceToken).where(DeviceToken.token == payload.token)))
        .scalars()
        .one_or_none()
    )

    if existing is not None:
        # Re-registering an existing token reassigns it. A shared device where one
        # user signs out and another signs in must not keep pushing to the first.
        existing.user_id = user_id
        existing.platform = payload.platform
        existing.is_active = True
        existing.last_seen_at = datetime.now(UTC)
        await session.commit()
        return DeviceTokenOut.model_validate(existing)

    device = DeviceToken(
        user_id=user_id,
        token=payload.token,
        platform=payload.platform,
        last_seen_at=datetime.now(UTC),
    )
    session.add(device)
    await session.commit()
    await session.refresh(device)
    return DeviceTokenOut.model_validate(device)


@router.delete(
    "/devices/{device_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Deregister a push token",
)
async def deregister_device(
    device_id: uuid.UUID,
    principal: CurrentUser,
    session: DbSession,
):  # type: ignore[no-untyped-def]
    from fastapi import Response

    from knowledgeos_core import NotFoundError

    device = await session.get(DeviceToken, device_id)
    if device is None or device.user_id != user_uuid(principal):
        raise NotFoundError("Device not found.")
    device.is_active = False
    await session.commit()
    return Response(status_code=status.HTTP_204_NO_CONTENT)


@router.get(
    "/channels",
    response_model=ChannelStatus,
    summary="Channels this deployment can use",
    description=(
        "So the preferences UI does not render a toggle for a channel with no provider behind it."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def channel_status() -> ChannelStatus:
    return ChannelStatus(
        channels=[NotificationChannel(name) for name in settings.enabled_channels],
        sending_enabled=settings.sending_enabled,
        email_provider=settings.email_provider,
    )
