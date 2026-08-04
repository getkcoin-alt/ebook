"""Authors, publishers and categories.

Reads are public and cached; writes require `books:write`.

These lists use **offset** pagination rather than the cursor paging the catalogue
feed uses. Taxonomies are small, near-static and usually rendered as a complete
alphabetical index, so a page number is what a caller actually wants — and the
"a new row shifts every page boundary" problem that makes offsets wrong for a feed
does not arise when a handful of authors are added a month.
"""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, Response, status

from deps import DbSession, Taxonomy
from knowledgeos_core import Page, PageParams, page_params
from knowledgeos_core.deps import rate_limit, require_permission
from schemas import (
    AuthorCreate,
    AuthorOut,
    AuthorUpdate,
    CategoryCreate,
    CategoryNode,
    CategoryOut,
    CategoryUpdate,
    PublisherCreate,
    PublisherOut,
    PublisherUpdate,
)

WRITE = Depends(require_permission("books:write"))
DELETE = Depends(require_permission("books:delete"))

authors_router = APIRouter(prefix="/v1/authors", tags=["authors"])
publishers_router = APIRouter(prefix="/v1/publishers", tags=["publishers"])
categories_router = APIRouter(prefix="/v1/categories", tags=["categories"])


# ---------------------------------------------------------------------------
# Authors
# ---------------------------------------------------------------------------


@authors_router.get(
    "",
    response_model=Page[AuthorOut],
    summary="List authors",
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def list_authors(
    session: DbSession,
    taxonomy: Taxonomy,
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=120)] = None,
    featured_only: Annotated[bool, Query()] = False,
) -> Page[AuthorOut]:
    rows, total = await taxonomy.list_authors(
        session, params=params, search=q, featured_only=featured_only
    )
    return Page[AuthorOut].create(
        [AuthorOut.model_validate(row) for row in rows], total=total, params=params
    )


@authors_router.get("/{slug}", response_model=AuthorOut, summary="Get an author by slug")
async def get_author(slug: str, session: DbSession, taxonomy: Taxonomy) -> AuthorOut:
    return AuthorOut.model_validate(await taxonomy.get_author_by_slug(session, slug))


@authors_router.post(
    "",
    response_model=AuthorOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create an author",
    dependencies=[WRITE],
)
async def create_author(payload: AuthorCreate, session: DbSession, taxonomy: Taxonomy) -> AuthorOut:
    return AuthorOut.model_validate(await taxonomy.create_author(session, payload))


@authors_router.patch(
    "/{author_id}", response_model=AuthorOut, summary="Update an author", dependencies=[WRITE]
)
async def update_author(
    author_id: uuid.UUID, payload: AuthorUpdate, session: DbSession, taxonomy: Taxonomy
) -> AuthorOut:
    author = await taxonomy.get_author(session, author_id)
    return AuthorOut.model_validate(await taxonomy.update_author(session, author, payload))


@authors_router.delete(
    "/{author_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete an author",
    dependencies=[DELETE],
)
async def delete_author(author_id: uuid.UUID, session: DbSession, taxonomy: Taxonomy) -> Response:
    await taxonomy.delete_author(session, await taxonomy.get_author(session, author_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Publishers
# ---------------------------------------------------------------------------


@publishers_router.get(
    "",
    response_model=Page[PublisherOut],
    summary="List publishers",
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def list_publishers(
    session: DbSession,
    taxonomy: Taxonomy,
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=120)] = None,
) -> Page[PublisherOut]:
    rows, total = await taxonomy.list_publishers(session, params=params, search=q)
    return Page[PublisherOut].create(
        [PublisherOut.model_validate(row) for row in rows], total=total, params=params
    )


@publishers_router.get("/{slug}", response_model=PublisherOut, summary="Get a publisher by slug")
async def get_publisher(slug: str, session: DbSession, taxonomy: Taxonomy) -> PublisherOut:
    return PublisherOut.model_validate(await taxonomy.get_publisher_by_slug(session, slug))


@publishers_router.post(
    "",
    response_model=PublisherOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a publisher",
    dependencies=[WRITE],
)
async def create_publisher(
    payload: PublisherCreate, session: DbSession, taxonomy: Taxonomy
) -> PublisherOut:
    return PublisherOut.model_validate(await taxonomy.create_publisher(session, payload))


@publishers_router.patch(
    "/{publisher_id}",
    response_model=PublisherOut,
    summary="Update a publisher",
    dependencies=[WRITE],
)
async def update_publisher(
    publisher_id: uuid.UUID, payload: PublisherUpdate, session: DbSession, taxonomy: Taxonomy
) -> PublisherOut:
    publisher = await taxonomy.get_publisher(session, publisher_id)
    return PublisherOut.model_validate(await taxonomy.update_publisher(session, publisher, payload))


@publishers_router.delete(
    "/{publisher_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a publisher",
    dependencies=[DELETE],
)
async def delete_publisher(
    publisher_id: uuid.UUID, session: DbSession, taxonomy: Taxonomy
) -> Response:
    await taxonomy.delete_publisher(session, await taxonomy.get_publisher(session, publisher_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)


# ---------------------------------------------------------------------------
# Categories
# ---------------------------------------------------------------------------


@categories_router.get(
    "",
    response_model=Page[CategoryOut],
    summary="List categories (flat)",
    description="The flat list. Use `/tree` for the hierarchy in one response.",
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def list_categories(
    session: DbSession,
    taxonomy: Taxonomy,
    params: Annotated[PageParams, Depends(page_params)],
    q: Annotated[str | None, Query(max_length=120)] = None,
) -> Page[CategoryOut]:
    rows, total = await taxonomy.list_categories(session, params=params, search=q)
    return Page[CategoryOut].create(
        [CategoryOut.model_validate(row) for row in rows], total=total, params=params
    )


@categories_router.get(
    "/tree",
    response_model=list[CategoryNode],
    summary="Category tree",
    description=(
        "The whole hierarchy in one response, built from a single query rather than "
        "one per level. Cached — the taxonomy changes far less often than it is read."
    ),
    dependencies=[Depends(rate_limit("anonymous"))],
)
async def category_tree(session: DbSession, taxonomy: Taxonomy) -> list[CategoryNode]:
    return await taxonomy.category_tree(session)


@categories_router.get("/{slug}", response_model=CategoryOut, summary="Get a category by slug")
async def get_category(slug: str, session: DbSession, taxonomy: Taxonomy) -> CategoryOut:
    return CategoryOut.model_validate(await taxonomy.get_category_by_slug(session, slug))


@categories_router.post(
    "",
    response_model=CategoryOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a category",
    dependencies=[WRITE],
)
async def create_category(
    payload: CategoryCreate, session: DbSession, taxonomy: Taxonomy
) -> CategoryOut:
    return CategoryOut.model_validate(await taxonomy.create_category(session, payload))


@categories_router.patch(
    "/{category_id}",
    response_model=CategoryOut,
    summary="Update a category",
    dependencies=[WRITE],
)
async def update_category(
    category_id: uuid.UUID, payload: CategoryUpdate, session: DbSession, taxonomy: Taxonomy
) -> CategoryOut:
    category = await taxonomy.get_category(session, category_id)
    return CategoryOut.model_validate(await taxonomy.update_category(session, category, payload))


@categories_router.delete(
    "/{category_id}",
    status_code=status.HTTP_204_NO_CONTENT,
    summary="Delete a category",
    dependencies=[DELETE],
)
async def delete_category(
    category_id: uuid.UUID, session: DbSession, taxonomy: Taxonomy
) -> Response:
    await taxonomy.delete_category(session, await taxonomy.get_category(session, category_id))
    return Response(status_code=status.HTTP_204_NO_CONTENT)
