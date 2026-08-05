"""User directory and audit search — the read side of the admin console.

Split from ``AccountService`` because the concerns genuinely differ. That service is
the authentication path: it registers, authenticates, and is on the hot path for every
sign-in. This one is a back-office reader, queried by a handful of operators, and its
queries are the shape you would never allow on a login path — substring search over
email, sorting by arbitrary columns, counting.

Two rules that matter more here than they look:

**An admin listing a user is an audited event.** Not the listing itself — that would
bury the log in noise — but every *action* taken from it. `set_ban` already records
one; this module exists so the console that triggers it has something to show.

**Search is a prefix/substring match on email and name only.** Never on anything that
identifies a person indirectly — a search over IP addresses or user agents turns a
support tool into a surveillance one, and nobody asks for that feature until it is
already there.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import Select, func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import get_logger
from models import AuditLog, User
from settings import Settings

logger = get_logger(__name__)

#: Columns an operator may sort by. An allowlist rather than passing the parameter
#: through: an unvalidated sort column is an injection point, and `ORDER BY
#: password_hash` is a way to read a hash one binary-search request at a time.
SORTABLE = {
    "created_at": User.created_at,
    "last_login_at": User.last_login_at,
    "email": User.email,
}


class DirectoryService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    def _filtered(self, stmt: Select, *, search: str | None, status: str | None) -> Select:
        if search:
            # Both sides wrapped: an operator looking for "smith" means the surname,
            # not addresses that start with it. Slow, and correct for a back office
            # over a table this size — if it ever stops being fast enough, the fix is
            # a trigram index, not a narrower search.
            term = f"%{search.strip().lower()}%"
            stmt = stmt.where(
                or_(func.lower(User.email).like(term), func.lower(User.full_name).like(term))
            )
        if status == "banned":
            stmt = stmt.where(User.banned_at.is_not(None))
        elif status == "active":
            stmt = stmt.where(User.banned_at.is_(None), User.deleted_at.is_(None))
        elif status == "unverified":
            stmt = stmt.where(User.email_verified_at.is_(None))
        elif status == "deleted":
            stmt = stmt.where(User.deleted_at.is_not(None))
        return stmt

    async def list_users(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        search: str | None = None,
        status: str | None = None,
        sort_by: str = "created_at",
        sort_order: str = "desc",
    ) -> tuple[list[User], int]:
        """A page of the directory, with a real total.

        Offset paging with a count, unlike the public feeds. A console needs to jump
        to page four and needs to know that a search matched 3 people rather than
        3,000 — and the cost of a count over this table, queried by a few operators,
        is nothing like the cost of one on an infinite scroll.
        """
        column = SORTABLE.get(sort_by, User.created_at)
        ordering = column.desc() if sort_order == "desc" else column.asc()

        total = int(
            (
                await session.execute(
                    self._filtered(select(func.count(User.id)), search=search, status=status)
                )
            ).scalar_one()
        )
        stmt = (
            self._filtered(select(User), search=search, status=status)
            # A stable tiebreak, so a page boundary does not shuffle rows between
            # requests when several share a timestamp.
            .order_by(ordering, User.id)
            .limit(limit)
            .offset(offset)
        )
        return list((await session.execute(stmt)).scalars().all()), total

    async def stats(self, session: AsyncSession, *, days: int = 30) -> dict[str, int]:
        """Headline counts for the users panel."""
        since = datetime.now(UTC) - timedelta(days=days)

        async def count(*conditions) -> int:  # type: ignore[no-untyped-def]
            stmt = select(func.count(User.id))
            for condition in conditions:
                stmt = stmt.where(condition)
            return int((await session.execute(stmt)).scalar_one())

        return {
            "total": await count(User.deleted_at.is_(None)),
            "new_in_window": await count(User.created_at >= since, User.deleted_at.is_(None)),
            "verified": await count(User.email_verified_at.is_not(None), User.deleted_at.is_(None)),
            "banned": await count(User.banned_at.is_not(None)),
            # Active means "signed in inside the window", which is the only definition
            # that does not quietly count people who registered years ago and left.
            "active_in_window": await count(User.last_login_at >= since, User.deleted_at.is_(None)),
        }

    async def audit_logs(
        self,
        session: AsyncSession,
        *,
        limit: int = 50,
        offset: int = 0,
        user_id: uuid.UUID | None = None,
        actor_id: uuid.UUID | None = None,
        action: str | None = None,
        days: int = 30,
    ) -> tuple[list[AuditLog], int]:
        """Search the audit log.

        Windowed by default. The table is the largest in this schema and an unbounded
        `ORDER BY created_at DESC` over all of it, for a console that almost always
        wants the last week, is a full scan to render a first page.
        """
        since = datetime.now(UTC) - timedelta(days=days)

        def apply(stmt: Select) -> Select:
            stmt = stmt.where(AuditLog.created_at >= since)
            if user_id is not None:
                stmt = stmt.where(AuditLog.user_id == user_id)
            if actor_id is not None:
                stmt = stmt.where(AuditLog.actor_id == actor_id)
            if action:
                stmt = stmt.where(AuditLog.action == action)
            return stmt

        total = int((await session.execute(apply(select(func.count(AuditLog.id))))).scalar_one())
        stmt = (
            apply(select(AuditLog))
            .order_by(AuditLog.created_at.desc(), AuditLog.id)
            .limit(limit)
            .offset(offset)
        )
        return list((await session.execute(stmt)).scalars().all()), total

    async def actions(self, session: AsyncSession, *, days: int = 30) -> list[str]:
        """Distinct action names in the window, to populate the filter dropdown.

        Read from the data rather than from a constant: a hard-coded list goes stale
        the first time somebody adds an audited action and forgets this file.
        """
        since = datetime.now(UTC) - timedelta(days=days)
        stmt = (
            select(AuditLog.action)
            .where(AuditLog.created_at >= since)
            .group_by(AuditLog.action)
            .order_by(AuditLog.action)
        )
        return [row[0] for row in (await session.execute(stmt)).all()]
