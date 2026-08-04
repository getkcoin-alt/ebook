"""The in-app notification list.

Read far more often than it is written — a bell icon polls the unread count on every
page — so the two operations are separate endpoints backed by separate queries. Making
the badge fetch the full list would be the single most expensive query on the
platform, run several times a minute per signed-in user.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime

from sqlalchemy import and_, delete, func, or_, select, update
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import (
    BadRequestError,
    NotFoundError,
    decode_cursor,
    encode_cursor,
    get_logger,
)
from models import Notification
from settings import Settings

logger = get_logger(__name__)


class InboxService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def unread_count(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        stmt = select(func.count(Notification.id)).where(
            Notification.user_id == user_id,
            Notification.read_at.is_(None),
            Notification.archived_at.is_(None),
        )
        return int((await session.execute(stmt)).scalar_one())

    async def list(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        cursor: str | None = None,
        limit: int = 20,
        unread_only: bool = False,
        include_archived: bool = False,
    ) -> tuple[list[Notification], str | None, bool]:
        """Keyset paginated on (created_at, id).

        Offset paging is wrong here for the usual reason and one extra: a new
        notification arriving while the user scrolls would shift every boundary,
        and this is a list that grows *while it is being read*.
        """
        conditions = [Notification.user_id == user_id]
        if unread_only:
            conditions.append(Notification.read_at.is_(None))
        if not include_archived:
            conditions.append(Notification.archived_at.is_(None))

        if cursor:
            last_created, last_id = _decode(cursor)
            conditions.append(
                or_(
                    Notification.created_at < last_created,
                    and_(Notification.created_at == last_created, Notification.id < last_id),
                )
            )

        stmt = (
            select(Notification)
            .where(*conditions)
            .order_by(Notification.created_at.desc(), Notification.id.desc())
            .limit(limit + 1)  # the extra row answers "is there more?"
        )
        rows = list((await session.execute(stmt)).scalars().all())
        has_more = len(rows) > limit
        rows = rows[:limit]
        next_cursor = (
            encode_cursor({"v": rows[-1].created_at.isoformat(), "id": str(rows[-1].id)})
            if rows and has_more
            else None
        )
        return rows, next_cursor, has_more

    async def get_for_user(
        self, session: AsyncSession, notification_id: uuid.UUID, user_id: uuid.UUID
    ) -> Notification:
        notification = await session.get(Notification, notification_id)
        # 404 rather than 403 for someone else's: a 403 confirms it exists.
        if notification is None or notification.user_id != user_id:
            raise NotFoundError(
                "Notification not found.", details={"notification_id": str(notification_id)}
            )
        return notification

    async def mark_read(
        self,
        session: AsyncSession,
        *,
        user_id: uuid.UUID,
        notification_ids: list[uuid.UUID] | None = None,
    ) -> int:
        """Mark some or all as read. Returns how many changed.

        A single UPDATE rather than a load-and-loop: "mark all read" on an account
        with a thousand unread notifications should be one statement, not a
        thousand round trips.
        """
        conditions = [Notification.user_id == user_id, Notification.read_at.is_(None)]
        if notification_ids:
            conditions.append(Notification.id.in_(notification_ids))

        result = await session.execute(
            update(Notification).where(*conditions).values(read_at=datetime.now(UTC))
        )
        await session.commit()
        return int(result.rowcount or 0)

    async def archive(self, session: AsyncSession, notification: Notification) -> Notification:
        """Hide from the list without deleting.

        The delivery rows reference it, and a support conversation about "I never
        got that email" needs the notification it was attached to.
        """
        notification.archived_at = datetime.now(UTC)
        if notification.read_at is None:
            # Archiving is an acknowledgement; leaving it unread would keep the
            # badge lit for something the user has explicitly dismissed.
            notification.read_at = notification.archived_at
        await session.commit()
        return notification

    async def prune(self, session: AsyncSession, user_id: uuid.UUID) -> int:
        """Keep one user's list bounded.

        An account that has been active for years otherwise accumulates an
        unbounded list nobody scrolls to the end of. The newest
        ``in_app_retention`` rows survive.
        """
        keep = self._settings.in_app_retention
        stmt = (
            select(Notification.id)
            .where(Notification.user_id == user_id)
            .order_by(Notification.created_at.desc())
            .offset(keep)
        )
        stale = list((await session.execute(stmt)).scalars().all())
        if not stale:
            return 0
        await session.execute(delete(Notification).where(Notification.id.in_(stale)))
        await session.commit()
        logger.info("notification.pruned", user_id=str(user_id), count=len(stale))
        return len(stale)


def _decode(cursor: str) -> tuple[datetime, uuid.UUID]:
    data = decode_cursor(cursor)
    try:
        created_at = datetime.fromisoformat(str(data["v"]))
        last_id = uuid.UUID(str(data["id"]))
    except (KeyError, ValueError) as exc:
        raise BadRequestError("The pagination cursor is malformed.") from exc
    # Deliberately *not* normalised to UTC. The value was produced by
    # `.isoformat()` on whatever the database returned, so it already matches that
    # dialect's representation — Postgres gives an aware datetime, SQLite a naive
    # one. Forcing tzinfo on makes the bound render as "...+00:00" while the stored
    # column has no offset, and on SQLite (which compares timestamps as strings)
    # the shorter stored value then sorts *before* every bound: the `<` matches
    # every row and the cursor returns page one forever.
    return created_at, last_id
