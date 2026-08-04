"""Authors, publishers and the category tree.

Reference data: written rarely by staff, read on nearly every page. The category
tree is therefore assembled once from a single flat SELECT and cached, rather than
being fetched with a recursive CTE per request — the tree is two or three levels
deep and a few hundred rows, so the whole thing fits comfortably in one cache entry.
"""

from __future__ import annotations

import uuid
from datetime import UTC, datetime
from typing import Any, TypeVar

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import (
    BadRequestError,
    ConflictError,
    NotFoundError,
    PageParams,
    apply_search,
    apply_sorting,
    get_logger,
    paginate,
)
from models import Author, Book, BookAuthor, BookCategory, Category, Publisher
from schemas import (
    AuthorCreate,
    AuthorUpdate,
    CategoryCreate,
    CategoryNode,
    CategoryUpdate,
    PublisherCreate,
    PublisherUpdate,
)
from services.cache import CatalogueCache
from settings import Settings

logger = get_logger(__name__)

ModelT = TypeVar("ModelT", Author, Publisher, Category)

#: Guards against a cycle introduced by re-parenting, and against a tree so deep
#: that the flat-select-and-assemble approach stops being cheap.
MAX_CATEGORY_DEPTH = 5


class TaxonomyService:
    def __init__(self, settings: Settings, cache: CatalogueCache) -> None:
        self._settings = settings
        self._cache = cache

    # ---- shared helpers -------------------------------------------------

    @staticmethod
    async def _get(session: AsyncSession, model: type[ModelT], entity_id: uuid.UUID) -> ModelT:
        entity = await session.get(model, entity_id)
        if entity is None or entity.deleted_at is not None:
            raise NotFoundError(f"{model.__name__} not found.", details={"id": str(entity_id)})
        return entity

    @staticmethod
    async def _by_slug(session: AsyncSession, model: type[ModelT], slug: str) -> ModelT:
        entity = (
            await session.execute(
                select(model).where(model.slug == slug, model.deleted_at.is_(None))
            )
        ).scalars().one_or_none()
        if entity is None:
            raise NotFoundError(f"{model.__name__} not found.", details={"slug": slug})
        return entity

    @staticmethod
    async def _assert_slug_free(
        session: AsyncSession, model: type[ModelT], slug: str, *, exclude: uuid.UUID | None = None
    ) -> None:
        stmt = select(model.id).where(model.slug == slug)
        if exclude is not None:
            stmt = stmt.where(model.id != exclude)
        if (await session.execute(stmt.limit(1))).scalar_one_or_none() is not None:
            raise ConflictError("That slug is already taken.", details={"slug": slug})

    @staticmethod
    async def _paged(
        session: AsyncSession,
        model: type[ModelT],
        *,
        params: PageParams,
        search: str | None,
        search_fields: list[str],
        sort_by: str,
        sort_order: str,
        allowed: set[str],
        extra: Any = None,
    ) -> tuple[list[ModelT], int]:
        stmt = select(model).where(model.deleted_at.is_(None))
        if extra is not None:
            stmt = stmt.where(extra)
        if search:
            stmt = apply_search(stmt, model, term=search, fields=search_fields)
        stmt = apply_sorting(
            stmt, model, sort_by=sort_by, sort_order=sort_order, allowed=allowed
        )
        return await paginate(session, stmt, params)

    # ---- authors --------------------------------------------------------

    async def list_authors(
        self,
        session: AsyncSession,
        *,
        params: PageParams,
        search: str | None = None,
        featured_only: bool = False,
        sort_by: str = "name",
        sort_order: str = "asc",
    ) -> tuple[list[Author], int]:
        return await self._paged(
            session,
            Author,
            params=params,
            search=search,
            search_fields=["name"],
            sort_by=sort_by,
            sort_order=sort_order,
            allowed={"name", "created_at"},
            extra=Author.is_featured.is_(True) if featured_only else None,
        )

    async def get_author(self, session: AsyncSession, author_id: uuid.UUID) -> Author:
        return await self._get(session, Author, author_id)

    async def get_author_by_slug(self, session: AsyncSession, slug: str) -> Author:
        return await self._by_slug(session, Author, slug)

    async def create_author(self, session: AsyncSession, payload: AuthorCreate) -> Author:
        await self._assert_slug_free(session, Author, payload.slug)
        author = Author(**payload.model_dump())
        session.add(author)
        await self._flush(session, "That slug is already taken.")
        return author

    async def update_author(
        self, session: AsyncSession, author: Author, payload: AuthorUpdate
    ) -> Author:
        data = payload.model_dump(exclude_unset=True)
        if "slug" in data:
            await self._assert_slug_free(session, Author, data["slug"], exclude=author.id)
        for field, value in data.items():
            setattr(author, field, value)
        await self._flush(session, "That slug is already taken.")
        return author

    async def delete_author(self, session: AsyncSession, author: Author) -> None:
        linked = (
            await session.execute(
                select(BookAuthor.book_id).where(BookAuthor.author_id == author.id).limit(1)
            )
        ).scalar_one_or_none()
        if linked is not None:
            raise ConflictError(
                "This author is still credited on at least one book.",
                details={"author_id": str(author.id)},
            )
        author.deleted_at = datetime.now(UTC)
        await session.flush()

    # ---- publishers -----------------------------------------------------

    async def list_publishers(
        self,
        session: AsyncSession,
        *,
        params: PageParams,
        search: str | None = None,
        sort_by: str = "name",
        sort_order: str = "asc",
    ) -> tuple[list[Publisher], int]:
        return await self._paged(
            session,
            Publisher,
            params=params,
            search=search,
            search_fields=["name"],
            sort_by=sort_by,
            sort_order=sort_order,
            allowed={"name", "created_at"},
        )

    async def get_publisher(self, session: AsyncSession, publisher_id: uuid.UUID) -> Publisher:
        return await self._get(session, Publisher, publisher_id)

    async def get_publisher_by_slug(self, session: AsyncSession, slug: str) -> Publisher:
        return await self._by_slug(session, Publisher, slug)

    async def create_publisher(self, session: AsyncSession, payload: PublisherCreate) -> Publisher:
        await self._assert_slug_free(session, Publisher, payload.slug)
        publisher = Publisher(**payload.model_dump())
        session.add(publisher)
        await self._flush(session, "That slug is already taken.")
        return publisher

    async def update_publisher(
        self, session: AsyncSession, publisher: Publisher, payload: PublisherUpdate
    ) -> Publisher:
        data = payload.model_dump(exclude_unset=True)
        if "slug" in data:
            await self._assert_slug_free(session, Publisher, data["slug"], exclude=publisher.id)
        for field, value in data.items():
            setattr(publisher, field, value)
        await self._flush(session, "That slug is already taken.")
        return publisher

    async def delete_publisher(self, session: AsyncSession, publisher: Publisher) -> None:
        linked = (
            await session.execute(
                select(Book.id).where(Book.publisher_id == publisher.id).limit(1)
            )
        ).scalar_one_or_none()
        if linked is not None:
            raise ConflictError(
                "This publisher still has books in the catalogue.",
                details={"publisher_id": str(publisher.id)},
            )
        publisher.deleted_at = datetime.now(UTC)
        await session.flush()

    # ---- categories -----------------------------------------------------

    async def list_categories(
        self,
        session: AsyncSession,
        *,
        params: PageParams,
        search: str | None = None,
        sort_by: str = "display_order",
        sort_order: str = "asc",
    ) -> tuple[list[Category], int]:
        return await self._paged(
            session,
            Category,
            params=params,
            search=search,
            search_fields=["name"],
            sort_by=sort_by,
            sort_order=sort_order,
            allowed={"name", "display_order", "created_at"},
        )

    async def get_category(self, session: AsyncSession, category_id: uuid.UUID) -> Category:
        return await self._get(session, Category, category_id)

    async def get_category_by_slug(self, session: AsyncSession, slug: str) -> Category:
        return await self._by_slug(session, Category, slug)

    async def create_category(self, session: AsyncSession, payload: CategoryCreate) -> Category:
        await self._assert_slug_free(session, Category, payload.slug)
        if payload.parent_id is not None:
            await self._get(session, Category, payload.parent_id)
        category = Category(**payload.model_dump())
        session.add(category)
        await self._flush(session, "That slug is already taken.")
        await self._cache.invalidate_category_tree()
        return category

    async def update_category(
        self, session: AsyncSession, category: Category, payload: CategoryUpdate
    ) -> Category:
        data = payload.model_dump(exclude_unset=True)
        if "slug" in data:
            await self._assert_slug_free(session, Category, data["slug"], exclude=category.id)
        if "parent_id" in data and data["parent_id"] is not None:
            await self._assert_no_cycle(session, category, data["parent_id"])
        for field, value in data.items():
            setattr(category, field, value)
        await self._flush(session, "That slug is already taken.")
        await self._cache.invalidate_category_tree()
        return category

    async def delete_category(self, session: AsyncSession, category: Category) -> None:
        linked = (
            await session.execute(
                select(BookCategory.book_id)
                .where(BookCategory.category_id == category.id)
                .limit(1)
            )
        ).scalar_one_or_none()
        if linked is not None:
            raise ConflictError(
                "This category still has books in it.",
                details={"category_id": str(category.id)},
            )
        category.deleted_at = datetime.now(UTC)
        await session.flush()
        await self._cache.invalidate_category_tree()

    async def _assert_no_cycle(
        self, session: AsyncSession, category: Category, parent_id: uuid.UUID
    ) -> None:
        """Walk up from the proposed parent; meeting this category means a cycle.

        A cycle would make the tree builder recurse forever and take the whole
        catalogue down with it, so it is rejected at the write.
        """
        if parent_id == category.id:
            raise BadRequestError("A category cannot be its own parent.")
        current: uuid.UUID | None = parent_id
        for _ in range(MAX_CATEGORY_DEPTH + 1):
            if current is None:
                return
            if current == category.id:
                raise BadRequestError("That parent would create a cycle in the category tree.")
            parent = await session.get(Category, current)
            if parent is None or parent.deleted_at is not None:
                raise NotFoundError("Parent category not found.", details={"id": str(current)})
            current = parent.parent_id
        raise BadRequestError(
            "The category tree cannot be deeper than "
            f"{MAX_CATEGORY_DEPTH} levels.",
        )

    async def category_tree(self, session: AsyncSession) -> list[CategoryNode]:
        """The active category tree, served from cache when warm."""
        cached = await self._cache.get_category_tree()
        if cached is not None:
            return [CategoryNode.model_validate(node) for node in cached]

        rows = list(
            (
                await session.execute(
                    select(Category)
                    .where(Category.deleted_at.is_(None), Category.is_active.is_(True))
                    .order_by(Category.display_order, Category.name)
                )
            )
            .scalars()
            .all()
        )
        tree = self._assemble(rows)
        await self._cache.set_category_tree(
            [node.model_dump(mode="json") for node in tree]
        )
        return tree

    @staticmethod
    def _assemble(rows: list[Category]) -> list[CategoryNode]:
        """One pass to build the nodes, one to link them. No recursion into the DB."""
        nodes = {row.id: CategoryNode.model_validate(row) for row in rows}
        roots: list[CategoryNode] = []
        for row in rows:
            node = nodes[row.id]
            parent = nodes.get(row.parent_id) if row.parent_id else None
            if parent is None:
                # A child whose parent is inactive or deleted is promoted to a root
                # rather than disappearing from navigation entirely.
                roots.append(node)
            else:
                parent.children.append(node)
        return roots

    # ---- misc -----------------------------------------------------------

    @staticmethod
    async def _flush(session: AsyncSession, conflict_message: str) -> None:
        try:
            async with session.begin_nested():
                await session.flush()
        except IntegrityError as exc:
            raise ConflictError(conflict_message) from exc

