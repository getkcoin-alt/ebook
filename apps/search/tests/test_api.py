"""HTTP surface: public access, input validation, authorisation and degradation.

The search box is the most hostile input surface on the platform, so most of these
assert that a crafted request is a clean 400 from us rather than a 500 from an
engine we handed it to.
"""

from __future__ import annotations

import uuid

from tests.conftest import book_document, catalogue_book

# ---------------------------------------------------------------------------
# Public access
# ---------------------------------------------------------------------------


async def test_search_is_public(client, settings, meili_engine):
    """Search is how someone decides whether to sign up; requiring an account
    first is backwards."""
    meili_engine.add(settings.books_index, book_document(title="Dune"))
    response = await client.get("/v1/search?q=dune")
    assert response.status_code == 200
    assert response.json()["hits"][0]["title"] == "Dune"


async def test_suggest_and_trending_are_public(client):
    assert (await client.get("/v1/search/suggest?q=a")).status_code == 200
    assert (await client.get("/v1/search/trending")).status_code == 200


async def test_an_empty_query_returns_the_catalogue(client, settings, meili_engine):
    """A blank search is a browse, and it must still paginate and facet."""
    meili_engine.add(settings.books_index, book_document(), book_document(slug="b2"))
    response = await client.get("/v1/search")
    assert response.status_code == 200
    assert response.json()["estimated_total"] == 2


# ---------------------------------------------------------------------------
# Input validation
# ---------------------------------------------------------------------------


async def test_a_filter_must_be_name_colon_value(client):
    response = await client.get("/v1/search?filter=justastring")
    assert response.status_code == 400
    assert "allowed" in response.json()["error"]["details"]


async def test_an_unknown_filter_name_is_refused_by_name(client):
    response = await client.get("/v1/search?filter=secret:yes")
    assert response.status_code == 400
    assert "secret" in response.json()["error"]["message"]


async def test_repeated_filters_are_accepted(client, settings, meili_engine):
    """`?filter=category:a&filter=category:b` is what a plain HTML form produces."""
    meili_engine.add(
        settings.books_index,
        book_document(title="A", category_slugs=["fiction"]),
        book_document(title="B", category_slugs=["history"]),
        book_document(title="C", category_slugs=["poetry"]),
    )
    response = await client.get("/v1/search?filter=category:fiction&filter=category:history")
    titles = {hit["title"] for hit in response.json()["hits"]}
    assert titles == {"A", "B"}


async def test_an_unknown_sort_is_a_422(client):
    """A closed enum: the value selects a server-declared expression, so a
    free-form one could ask the engine to sort by an unsortable attribute."""
    response = await client.get("/v1/search?sort=by_secret_score")
    assert response.status_code == 422


async def test_an_inverted_price_range_is_a_400(client):
    response = await client.get("/v1/search?min_price_minor=50000&max_price_minor=100")
    assert response.status_code == 400


async def test_an_oversized_limit_is_a_422(client):
    response = await client.get("/v1/search?limit=100000")
    assert response.status_code == 422


async def test_a_filter_value_cannot_break_out_of_the_expression(client, settings, meili_engine):
    """The injection attempt that would otherwise surface drafts."""
    meili_engine.add(
        settings.books_index,
        book_document(title="Published"),
        book_document(title="Secret", status="draft"),
    )
    response = await client.get('/v1/search?filter=category:x" OR status = "draft')
    assert response.status_code == 200
    assert "Secret" not in {hit["title"] for hit in response.json()["hits"]}


async def test_an_overlong_query_is_a_422(client):
    response = await client.get(f"/v1/search?q={'x' * 500}")
    assert response.status_code == 422


# ---------------------------------------------------------------------------
# Pagination
# ---------------------------------------------------------------------------


async def test_a_cursor_walks_the_result_set(client, settings, meili_engine):
    meili_engine.add(
        settings.books_index, *[book_document(slug=f"b{n}", title=f"Book {n}") for n in range(5)]
    )
    first = (await client.get("/v1/search?limit=2")).json()
    assert first["has_more"] is True

    second = (await client.get(f"/v1/search?limit=2&cursor={first['next_cursor']}")).json()
    assert {h["id"] for h in first["hits"]}.isdisjoint({h["id"] for h in second["hits"]})


async def test_a_forged_cursor_is_a_400(client):
    response = await client.get("/v1/search?cursor=abc123")
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# Degradation
# ---------------------------------------------------------------------------


async def test_a_search_engine_outage_is_a_503_not_a_500(client, meili_engine):
    """A search outage must be a clean, retryable failure — not an unhandled one."""
    meili_engine.healthy = False
    response = await client.get("/v1/search?q=anything")
    assert response.status_code == 503
    assert response.json()["error"]["code"]


async def test_the_service_stays_live_while_the_engine_is_down(client, meili_engine):
    """Liveness must not depend on Meilisearch, or one engine blip restarts every
    replica instead of degrading one endpoint."""
    meili_engine.healthy = False
    assert (await client.get("/health")).status_code == 200


async def test_a_missing_index_is_an_empty_page_not_an_error(client):
    """Before the first reindex the index does not exist. A user searching then
    should see 'no results', not a broken page."""
    response = await client.get("/v1/search?q=anything")
    assert response.status_code == 200
    assert response.json()["hits"] == []


# ---------------------------------------------------------------------------
# Trending and clicks
# ---------------------------------------------------------------------------


async def test_searching_feeds_the_trending_list(client, settings, meili_engine):
    meili_engine.add(settings.books_index, book_document(title="Kubernetes"))
    for _ in range(3):
        await client.get("/v1/search?q=kubernetes")

    trending = (await client.get("/v1/search/trending")).json()
    assert trending["queries"][0]["query"] == "kubernetes"
    assert trending["queries"][0]["rank"] == 1


async def test_short_queries_do_not_reach_trending(client, settings, meili_engine):
    """Single letters are keystrokes, not intent, and would dominate the ranking."""
    meili_engine.add(settings.books_index, book_document())
    await client.get("/v1/search?q=a")
    assert (await client.get("/v1/search/trending")).json()["queries"] == []


async def test_a_search_returns_an_id_a_click_can_reference(client, settings, meili_engine):
    meili_engine.add(settings.books_index, book_document(title="Refactoring"))
    search = (await client.get("/v1/search?q=refactoring")).json()
    assert search["query_id"] is not None

    click = await client.post(
        "/v1/search/click",
        json={"query_id": search["query_id"], "book_id": search["hits"][0]["id"], "position": 0},
    )
    assert click.status_code == 201


async def test_a_click_on_an_unknown_search_is_a_404(client):
    response = await client.post(
        "/v1/search/click",
        json={"query_id": str(uuid.uuid4()), "book_id": "abc", "position": 0},
    )
    assert response.status_code == 404


# ---------------------------------------------------------------------------
# Related
# ---------------------------------------------------------------------------


async def test_related_excludes_the_source_book(client, settings, meili_engine):
    source = book_document(title="Source", category_slugs=["fiction"])
    meili_engine.add(
        settings.books_index, source, book_document(title="Other", category_slugs=["fiction"])
    )
    response = await client.get(f"/v1/search/related/{source['id']}")
    assert response.status_code == 200
    assert source["id"] not in [book["id"] for book in response.json()["books"]]


async def test_related_on_an_unknown_book_is_an_empty_list(client):
    response = await client.get(f"/v1/search/related/{uuid.uuid4()}")
    assert response.status_code == 200
    assert response.json()["books"] == []


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


async def test_admin_routes_reject_an_ordinary_user(client, as_user):
    as_user()
    assert (await client.get("/v1/admin/search/health")).status_code == 403
    assert (await client.post("/v1/admin/search/reindex", json={})).status_code == 403
    assert (await client.get("/v1/admin/search/analytics")).status_code == 403


async def test_an_admin_sees_index_health(client, as_admin, settings, meili_engine):
    meili_engine.add(settings.books_index, book_document())
    as_admin()
    response = await client.get("/v1/admin/search/health")
    assert response.status_code == 200
    assert response.json()["reachable"] is True


async def test_index_health_reports_an_outage_rather_than_raising(client, as_admin, meili_engine):
    meili_engine.healthy = False
    as_admin()
    response = await client.get("/v1/admin/search/health")
    assert response.status_code == 200
    assert response.json()["reachable"] is False


async def test_an_admin_can_reconcile(client, as_admin, catalogue, settings, meili_engine):
    catalogue.books.extend(catalogue_book(slug=f"b{n}") for n in range(2))
    as_admin()
    response = await client.post(
        "/v1/admin/search/reindex", json={"mode": "reconcile", "index": settings.books_index}
    )
    assert response.status_code == 200
    assert response.json()[0]["documents_indexed"] == 2
    assert len(meili_engine.indexes[settings.books_index]) == 2


async def test_reindexing_an_unknown_index_is_a_404(client, as_admin):
    """A silent no-op is far worse to debug than an error."""
    as_admin()
    response = await client.post("/v1/admin/search/reindex", json={"index": "kos_nonsense"})
    assert response.status_code == 404


async def test_reindex_runs_are_listed(client, as_admin, catalogue, settings):
    catalogue.books.append(catalogue_book())
    as_admin()
    await client.post("/v1/admin/search/reindex", json={"index": settings.books_index})

    runs = (await client.get("/v1/admin/search/runs")).json()
    assert runs["total"] == 1
    assert runs["items"][0]["status"] == "succeeded"


async def test_the_analytics_report_surfaces_zero_result_queries(
    client, as_admin, settings, meili_engine
):
    meili_engine.add(settings.books_index, book_document(title="Present"))
    await client.get("/v1/search?q=present")
    await client.get("/v1/search?q=absent")

    as_admin()
    report = (await client.get("/v1/admin/search/analytics")).json()
    assert report["total_searches"] == 2
    assert [stat["query"] for stat in report["zero_result_queries"]] == ["absent"]


async def test_trending_can_be_cleared(client, as_admin, settings, meili_engine):
    meili_engine.add(settings.books_index, book_document())
    await client.get("/v1/search?q=spamspamspam")
    assert (await client.get("/v1/search/trending")).json()["queries"]

    as_admin()
    assert (await client.delete("/v1/admin/search/trending")).status_code == 200
    assert (await client.get("/v1/search/trending")).json()["queries"] == []


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


async def test_internal_routes_reject_an_unsigned_caller(client):
    """Private-network reachability is not authorisation."""
    response = await client.post("/internal/documents", json={"documents": [{"id": "x"}]})
    assert response.status_code == 401


async def test_automation_can_push_a_document_directly(client, as_internal, settings, meili_engine):
    """The publish stage already holds the record; a round trip back to books
    would be pure latency."""
    as_internal("automation")
    document = book_document(title="Generated")
    response = await client.post("/internal/documents", json={"documents": [document]})

    assert response.status_code == 200
    assert response.json()["accepted"] == 1
    assert document["id"] in meili_engine.indexes[settings.books_index]


async def test_internal_delete_removes_a_document(client, as_internal, settings, meili_engine):
    document = book_document()
    meili_engine.add(settings.books_index, document)
    as_internal()
    response = await client.post(
        "/internal/documents/delete", json={"document_ids": [document["id"]]}
    )
    assert response.status_code == 200
    assert document["id"] not in meili_engine.indexes[settings.books_index]


async def test_pushing_to_an_unknown_index_is_a_404(client, as_internal):
    as_internal()
    response = await client.post(
        "/internal/documents", json={"index": "kos_nope", "documents": [{"id": "x"}]}
    )
    assert response.status_code == 404


async def test_the_worker_can_trigger_a_reconcile(client, as_internal, catalogue, settings):
    catalogue.books.append(catalogue_book())
    as_internal("worker")
    response = await client.post(
        "/internal/reindex", json={"mode": "reconcile", "index": settings.books_index}
    )
    assert response.status_code == 200
    assert response.json()[0]["status"] == "succeeded"


async def test_analytics_can_be_pruned(client, as_internal, settings, meili_engine, session):
    from sqlalchemy import func, select

    from models import SearchQuery

    meili_engine.add(settings.books_index, book_document())
    await client.get("/v1/search?q=something")

    as_internal("worker")
    response = await client.post("/internal/maintenance/prune-analytics?days=7")
    assert response.status_code == 200

    # Nothing is old enough yet, so the row survives.
    count = (await session.execute(select(func.count(SearchQuery.id)))).scalar_one()
    assert count == 1


async def test_health_and_metrics_are_public(client):
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/metrics")).status_code == 200
