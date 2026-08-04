"""Query construction and result mapping.

The bugs worth catching here are in the *request body* — a filter that is not
escaped, a sort expression the index does not support, a page offset that goes
deeper than the engine allows — and in the mapping back out. Those are the parts
this service owns; ranking is Meilisearch's job.
"""

from __future__ import annotations

import pytest

from knowledgeos_core import BadRequestError, encode_cursor
from schemas import SortOption
from services import SearchParams
from services.query import (
    FILTERABLE,
    SORT_EXPRESSIONS,
    assert_filters_are_indexable,
    escape_filter_value,
    normalise_query,
)
from tests.conftest import book_document

# ---------------------------------------------------------------------------
# Filter construction
# ---------------------------------------------------------------------------


def test_every_filter_the_api_accepts_is_declared_filterable():
    """The guard that stops a facet shipping as a 500.

    Meilisearch rejects a filter on an attribute that was never made filterable,
    and that rejection surfaces on a live search rather than at deploy time.
    """
    assert_filters_are_indexable()


def test_filter_values_are_quoted_and_escaped():
    """A value containing a quote must not break out of the filter string."""
    assert escape_filter_value("sci-fi") == '"sci-fi"'
    escaped = escape_filter_value('a" OR status = "draft')
    assert escaped.startswith('"') and escaped.endswith('"')
    assert escaped.count('"') == 2 + escaped.count('\\"')


def test_backslashes_are_escaped_before_quotes():
    r"""Reversing the order would double-escape the backslashes the quote
    replacement introduces, letting a crafted value break out."""
    assert escape_filter_value("back\\slash") == '"back\\\\slash"'


def test_published_status_is_applied_unconditionally(settings, meili):
    """No combination of query parameters may surface a draft."""
    from services import QueryService

    service = QueryService(settings, meili)
    clauses = service.build_filter(SearchParams(query="anything"))
    assert 'status = "published"' in clauses


def test_an_unknown_filter_name_is_a_400_naming_the_allowed_set(settings, meili):
    from services import QueryService

    service = QueryService(settings, meili)
    with pytest.raises(BadRequestError) as exc:
        service.build_filter(SearchParams(filters={"secret": ["yes"]}))
    assert set(exc.value.details["allowed"]) == set(FILTERABLE)


def test_same_filter_ors_and_different_filters_and(settings, meili):
    """What a facet sidebar means when a user ticks two categories and a language."""
    from services import QueryService

    service = QueryService(settings, meili)
    clauses = service.build_filter(
        SearchParams(filters={"category": ["fiction", "history"], "language": ["en"]})
    )
    ors = [clause for clause in clauses if isinstance(clause, list)]
    assert any(len(clause) == 2 for clause in ors)  # the two categories, ORed
    assert any(len(clause) == 1 for clause in ors)  # the language, ANDed in


def test_free_only_replaces_the_price_range(settings, meili):
    """Asking for free books and a price floor at once is contradictory; free wins."""
    from services import QueryService

    service = QueryService(settings, meili)
    clauses = service.build_filter(
        SearchParams(free_only=True, min_price_minor=100, max_price_minor=500)
    )
    assert "is_free = true" in clauses
    assert not any("price_minor" in str(clause) for clause in clauses)


def test_every_sort_option_has_an_expression():
    """A closed enum is only safe if every member maps to something."""
    assert set(SORT_EXPRESSIONS) == set(SortOption)


def test_relevance_sends_no_sort_expression(settings, meili):
    """Sorting by an attribute disables Meilisearch's relevance ranking entirely."""
    from services import QueryService

    service = QueryService(settings, meili)
    assert "sort" not in service.build_body(SearchParams(sort=SortOption.RELEVANCE))
    assert service.build_body(SearchParams(sort=SortOption.NEWEST))["sort"] == [
        "published_at_ts:desc"
    ]


def test_the_body_asks_for_one_document_more_than_the_limit(settings, meili):
    """That extra row answers 'is there a next page?' without a second query."""
    from services import QueryService

    service = QueryService(settings, meili)
    assert service.build_body(SearchParams(limit=20))["limit"] == 21


def test_the_limit_is_capped_at_the_configured_maximum(settings, meili):
    from services import QueryService

    service = QueryService(settings, meili)
    body = service.build_body(SearchParams(limit=10_000))
    assert body["limit"] == settings.search_max_limit + 1


def test_a_malformed_cursor_is_a_400(settings, meili):
    from services import QueryService

    service = QueryService(settings, meili)
    with pytest.raises(BadRequestError):
        service.build_body(SearchParams(cursor="not-a-cursor"))


def test_paging_too_deep_is_refused_with_an_explanation(settings, meili):
    """Meilisearch caps how deep it will scan; a silent empty page is worse than a 400."""
    from services import QueryService

    service = QueryService(settings, meili)
    with pytest.raises(BadRequestError) as exc:
        service.build_body(SearchParams(cursor=encode_cursor({"o": 99_999})))
    assert exc.value.code == "cursor_too_deep"


# ---------------------------------------------------------------------------
# Execution
# ---------------------------------------------------------------------------


async def test_a_search_returns_mapped_hits(settings, meili, meili_engine):
    from services import QueryService

    meili_engine.add(settings.books_index, book_document(title="Deep Learning"))
    response = await QueryService(settings, meili).search(SearchParams(query="deep"))

    assert response.estimated_total == 1
    assert response.hits[0].title == "Deep Learning"
    assert response.has_more is False
    assert response.next_cursor is None


async def test_filters_actually_narrow_the_result_set(settings, meili, meili_engine):
    from services import QueryService

    meili_engine.add(
        settings.books_index,
        book_document(title="A", category_slugs=["fiction"]),
        book_document(title="B", category_slugs=["history"]),
    )
    response = await QueryService(settings, meili).search(
        SearchParams(filters={"category": ["history"]})
    )
    assert [hit.title for hit in response.hits] == ["B"]


async def test_drafts_never_appear_in_results(settings, meili, meili_engine):
    from services import QueryService

    meili_engine.add(
        settings.books_index,
        book_document(title="Published"),
        book_document(title="Draft", status="draft"),
    )
    response = await QueryService(settings, meili).search(SearchParams())
    assert [hit.title for hit in response.hits] == ["Published"]


async def test_a_full_page_reports_more_and_returns_a_cursor(settings, meili, meili_engine):
    from services import QueryService

    meili_engine.add(settings.books_index, *[book_document(title=f"Book {n}") for n in range(5)])
    response = await QueryService(settings, meili).search(SearchParams(limit=2))

    assert len(response.hits) == 2  # the extra row is trimmed, not returned
    assert response.has_more is True
    assert response.next_cursor is not None


async def test_the_cursor_advances_to_the_next_page(settings, meili, meili_engine):
    from services import QueryService

    service = QueryService(settings, meili)
    meili_engine.add(settings.books_index, *[book_document(title=f"Book {n}") for n in range(5)])
    first = await service.search(SearchParams(limit=2))
    second = await service.search(SearchParams(limit=2, cursor=first.next_cursor))

    assert {hit.title for hit in first.hits}.isdisjoint({hit.title for hit in second.hits})


async def test_facet_counts_are_returned_with_the_page(settings, meili, meili_engine):
    from services import QueryService

    meili_engine.add(
        settings.books_index,
        book_document(title="A", category_slugs=["fiction"]),
        book_document(title="B", category_slugs=["fiction"]),
        book_document(title="C", category_slugs=["history"]),
    )
    response = await QueryService(settings, meili).search(SearchParams(facets=True))
    counts = {facet.value: facet.count for facet in response.facets.categories}
    assert counts == {"fiction": 2, "history": 1}


async def test_facets_can_be_switched_off(settings, meili, meili_engine):
    """Every facet is work; a page that does not render them should not ask."""
    from services import QueryService

    meili_engine.add(settings.books_index, book_document())
    await QueryService(settings, meili).search(SearchParams(facets=False))
    body = meili_engine.bodies("/search")[-1]
    assert "facets" not in body


# ---------------------------------------------------------------------------
# Semantic search
# ---------------------------------------------------------------------------


async def test_semantic_search_is_off_unless_configured(settings, meili, meili_engine):
    from services import QueryService

    meili_engine.add(settings.books_index, book_document())
    response = await QueryService(settings, meili).search(SearchParams(semantic=True))

    # The request opted in, but the deployment has no embedder — so no hybrid block
    # and an honest `semantic: false` in the response.
    assert "hybrid" not in meili_engine.bodies("/search")[-1]
    assert response.semantic is False


async def test_enabling_semantic_search_adds_a_hybrid_block(
    settings, meili, meili_engine, monkeypatch
):
    """Turning it on is a config flip, and turning it off is the same flip back."""
    from services import QueryService

    monkeypatch.setattr(settings, "semantic_enabled", True)
    meili_engine.add(settings.books_index, book_document())
    response = await QueryService(settings, meili).search(SearchParams())

    hybrid = meili_engine.bodies("/search")[-1]["hybrid"]
    assert hybrid["semanticRatio"] == settings.semantic_ratio
    assert hybrid["embedder"] == settings.semantic_embedder
    assert response.semantic is True


# ---------------------------------------------------------------------------
# Related and suggest
# ---------------------------------------------------------------------------


async def test_related_never_returns_the_source_book(settings, meili, meili_engine):
    from services import QueryService

    source = book_document(title="Source", category_slugs=["fiction"])
    meili_engine.add(
        settings.books_index, source, book_document(title="Other", category_slugs=["fiction"])
    )
    _reason, books = await QueryService(settings, meili).related(source["id"], 10)
    assert source["id"] not in [book.id for book in books]


async def test_related_on_an_unknown_book_is_empty_not_an_error(settings, meili):
    from services import QueryService

    reason, books = await QueryService(settings, meili).related("does-not-exist", 10)
    assert books == []
    assert reason == "similar"


async def test_suggest_queries_all_three_indexes_in_one_round_trip(settings, meili, meili_engine):
    """Three sequential queries would be three times the latency, per keystroke."""
    from services import QueryService

    meili_engine.add(settings.books_index, book_document(title="Python Crash Course"))
    meili_engine.add(
        settings.authors_index,
        {"id": "a1", "slug": "python-press", "name": "Python Press", "book_count": 3},
    )
    meili_engine.add(settings.categories_index, {"id": "c1", "slug": "python", "name": "Python"})

    response = await QueryService(settings, meili).suggest("python", 9)
    kinds = {suggestion.kind for suggestion in response.suggestions}
    assert kinds == {"book", "author", "category"}
    assert len(meili_engine.bodies("/multi-search")) == 1


async def test_suggest_on_an_empty_query_does_no_work(settings, meili, meili_engine):
    from services import QueryService

    response = await QueryService(settings, meili).suggest("   ")
    assert response.suggestions == []
    assert meili_engine.requests == []


async def test_suggest_links_point_at_real_routes(settings, meili, meili_engine):
    from services import QueryService

    meili_engine.add(settings.books_index, book_document(slug="deep-work", title="Deep Work"))
    response = await QueryService(settings, meili).suggest("deep")
    assert response.suggestions[0].href == "/books/deep-work"


# ---------------------------------------------------------------------------
# Normalisation
# ---------------------------------------------------------------------------


def test_query_normalisation_collapses_case_and_whitespace():
    """Grouping on raw text would report these as three different searches."""
    assert normalise_query("Machine  Learning") == "machine learning"
    assert normalise_query("  machine learning ") == "machine learning"
    assert normalise_query("MACHINE\tLEARNING") == "machine learning"
