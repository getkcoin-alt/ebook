"""Search analytics: what people looked for, and what they found.

The zero-result report is the reason this exists. What customers search for and do
*not* find is the highest-signal product feedback the platform produces — it is a
list of books to acquire, synonyms to add, and spellings to handle, written by the
people who wanted to give you money.

Two rules shape the implementation:

**Recording never fails a search.** Every write here is best-effort and swallows its
exceptions. A search that returned perfectly good results must not 500 because the
analytics table is locked or the disk is full.

**Aggregation happens in SQL.** Loading a month of queries into Python to count them
works on a demo dataset and falls over the first time someone opens the dashboard on
a busy week.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime, timedelta

from sqlalchemy import case, func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from knowledgeos_core import NotFoundError, get_logger
from models import SearchClick, SearchQuery
from schemas import AnalyticsResponse, QueryStat
from services.query import normalise_query
from settings import Settings

logger = get_logger(__name__)


class AnalyticsService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def enabled(self) -> bool:
        return self._settings.analytics_enabled

    # ---- recording ------------------------------------------------------

    async def record_query(
        self,
        sessionmaker: async_sessionmaker[AsyncSession] | None,
        *,
        query: str,
        filters: dict,
        sort: str | None,
        result_count: int,
        took_ms: int,
        user_id: uuid.UUID | None = None,
        session_id: str | None = None,
    ) -> uuid.UUID | None:
        """Store one executed query. Returns its id, or ``None`` if not recorded.

        The id is handed back to the client so a later click can be attributed to
        the search that produced it — that pairing is what makes click-through
        meaningful, and there is no way to reconstruct it afterwards.

        Uses its own session rather than the request's: this is a side effect, and
        it must not be rolled back along with, or hold a lock during, anything the
        request itself is doing.
        """
        if not self.enabled or sessionmaker is None:
            return None

        text = (query or "").strip()
        if not text:
            # An empty query is a browse, not a search. Recording it would swamp
            # the top-queries report with a blank row.
            return None

        try:
            async with sessionmaker() as session:
                row = SearchQuery(
                    query_text=text[: self._settings.analytics_max_query_length],
                    normalized_query=normalise_query(text)[
                        : self._settings.analytics_max_query_length
                    ],
                    filters=filters or {},
                    sort=sort,
                    result_count=result_count,
                    has_results=result_count > 0,
                    took_ms=took_ms,
                    user_id=user_id,
                    session_id=(session_id or None) and str(session_id)[:64],
                )
                session.add(row)
                await session.commit()
                return row.id
        except Exception as exc:
            # Analytics is never worth failing a search over.
            logger.warning("search.analytics_write_failed", error=str(exc))
            return None

    async def record_click(
        self,
        session: AsyncSession,
        *,
        query_id: uuid.UUID,
        book_id: str,
        position: int,
    ) -> SearchClick:
        """Attribute a result click to the query that produced it.

        Unlike ``record_query`` this one *does* raise: the client asked for it
        explicitly, so an unknown query id is a real error worth reporting rather
        than a silent no-op.
        """
        parent = await session.get(SearchQuery, query_id)
        if parent is None:
            raise NotFoundError("No search with that id.", details={"query_id": str(query_id)})
        click = SearchClick(query_id=query_id, book_id=book_id[:64], position=position)
        session.add(click)
        await session.commit()
        return click

    # ---- reporting ------------------------------------------------------

    async def report(
        self, session: AsyncSession, *, days: int = 7, top: int = 20
    ) -> AnalyticsResponse:
        since = datetime.now(UTC) - timedelta(days=days)
        window = [SearchQuery.created_at >= since]

        totals = (
            await session.execute(
                select(
                    func.count(SearchQuery.id),
                    func.count(func.distinct(SearchQuery.normalized_query)),
                    # SUM over an empty window is NULL, which would propagate through
                    # every derived figure below as None.
                    func.coalesce(
                        func.sum(case((SearchQuery.has_results.is_(False), 1), else_=0)), 0
                    ),
                    func.coalesce(func.avg(SearchQuery.took_ms), 0),
                ).where(*window)
            )
        ).one()
        total, unique, zero_results, avg_took = totals
        total = int(total)

        return AnalyticsResponse(
            window_days=days,
            total_searches=total,
            unique_queries=int(unique),
            zero_result_searches=int(zero_results),
            zero_result_rate=(int(zero_results) / total) if total else 0.0,
            average_took_ms=float(avg_took or 0),
            top_queries=await self._group(session, window, top=top, zero_only=False),
            zero_result_queries=await self._group(session, window, top=top, zero_only=True),
        )

    async def _group(
        self, session: AsyncSession, window: list, *, top: int, zero_only: bool
    ) -> list[QueryStat]:
        conditions = [*window]
        if zero_only:
            conditions.append(SearchQuery.has_results.is_(False))

        stmt = (
            select(
                SearchQuery.normalized_query,
                func.count(SearchQuery.id),
                func.coalesce(func.avg(SearchQuery.result_count), 0),
                func.coalesce(func.sum(case((SearchQuery.has_results.is_(False), 1), else_=0)), 0),
            )
            .where(*conditions)
            .group_by(SearchQuery.normalized_query)
            .order_by(func.count(SearchQuery.id).desc())
            .limit(top)
        )
        rows = (await session.execute(stmt)).all()
        return [
            QueryStat(
                query=query,
                searches=int(searches),
                average_results=float(average or 0),
                zero_result_rate=(int(zeroes) / int(searches)) if searches else 0.0,
            )
            for query, searches, average, zeroes in rows
        ]

    async def prune(self, session: AsyncSession, *, older_than_days: int = 90) -> int:
        """Drop queries past their useful life.

        Analytics rows accumulate faster than anything else in this service — one
        per keystroke-completed search — and a year-old query tells you nothing a
        month-old one does not. Clicks cascade with their parent.
        """
        cutoff = datetime.now(UTC) - timedelta(days=older_than_days)
        stale = (
            (await session.execute(select(SearchQuery).where(SearchQuery.created_at < cutoff)))
            .scalars()
            .all()
        )
        for row in stale:
            await session.delete(row)
        if stale:
            await session.commit()
            logger.info("search.analytics_pruned", count=len(stale))
        return len(stale)
