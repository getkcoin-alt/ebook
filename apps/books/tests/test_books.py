"""Book service behaviour: entitlement gating, reviews, pagination, ownership."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from knowledgeos_core import ForbiddenError, PaymentRequiredError
from tests.conftest import OTHER_ID, READER_ID

pytestmark = pytest.mark.asyncio


class TestCatalogue:
    async def test_published_books_are_listed(self, client, book_factory):
        await book_factory(title="Visible Book")
        response = await client.get("/v1/books")
        assert response.status_code == 200
        assert [b["title"] for b in response.json()["items"]] == ["Visible Book"]

    async def test_drafts_are_hidden_from_the_public_feed(self, client, book_factory):
        await book_factory(title="Draft", status="draft", published_at=None)
        await book_factory(title="Live")
        titles = [b["title"] for b in (await client.get("/v1/books")).json()["items"]]
        assert titles == ["Live"]

    async def test_draft_slug_is_not_probeable(self, client, book_factory):
        book = await book_factory(status="draft", published_at=None)
        # A 404 rather than a 403: confirming the slug exists would leak the
        # unreleased title.
        assert (await client.get(f"/v1/books/{book.slug}")).status_code == 404

    async def test_staff_can_see_a_draft_at_its_canonical_url(self, client, book_factory, as_admin):
        book = await book_factory(status="draft", published_at=None)
        as_admin()
        assert (await client.get(f"/v1/books/{book.slug}")).status_code == 200

    async def test_get_by_slug(self, client, book_factory):
        book = await book_factory(title="Findable")
        body = (await client.get(f"/v1/books/{book.slug}")).json()
        assert body["title"] == "Findable"
        assert body["price_minor"] == 49900  # integer minor units, never a float

    async def test_missing_slug_is_404(self, client):
        assert (await client.get("/v1/books/no-such-book")).status_code == 404

    async def test_price_filters_apply_server_side(self, client, book_factory):
        await book_factory(title="Cheap", price_minor=10000)
        await book_factory(title="Pricey", price_minor=99900)
        body = (await client.get("/v1/books?max_price_minor=50000")).json()
        assert [b["title"] for b in body["items"]] == ["Cheap"]

    async def test_free_only_filter(self, client, book_factory):
        await book_factory(title="Free", price_minor=0)
        await book_factory(title="Paid", price_minor=49900)
        body = (await client.get("/v1/books?free_only=true")).json()
        assert [b["title"] for b in body["items"]] == ["Free"]

    async def test_invalid_sort_is_rejected_not_ignored(self, client):
        # The allowed set is a whitelist; user input selects from it and is never
        # interpolated into SQL.
        response = await client.get("/v1/books?sort_by=; DROP TABLE books")
        assert response.status_code == 400
        assert "allowed" in response.json()["error"]["details"]


class TestCursorPagination:
    async def test_cursor_walks_the_whole_set_without_gaps_or_repeats(self, client, book_factory):
        for i in range(7):
            await book_factory(title=f"Book {i:02d}")

        seen: list[str] = []
        cursor = None
        for _ in range(10):  # generous bound; the loop breaks on has_more
            url = f"/v1/books?limit=3{f'&cursor={cursor}' if cursor else ''}"
            body = (await client.get(url)).json()
            seen.extend(b["id"] for b in body["items"])
            cursor = body["next_cursor"]
            if not body["has_more"]:
                break

        assert len(seen) == 7
        assert len(set(seen)) == 7, "cursor pagination returned a duplicate"

    async def test_limit_is_capped(self, client, book_factory):
        await book_factory()
        # Above the maximum is a validation error, not a silent full-table scan.
        assert (await client.get("/v1/books?limit=100000")).status_code == 422

    async def test_malformed_cursor_is_a_client_error(self, client, book_factory):
        await book_factory()
        response = await client.get("/v1/books?cursor=not-a-real-cursor")
        assert response.status_code == 400


class TestEntitlementGating:
    async def test_download_without_entitlement_is_402(self, client, book_factory, as_user):
        book = await book_factory()
        as_user()
        response = await client.get(f"/v1/books/{book.id}/download")
        # 402, not 403: the book is purchasable, and the UI should offer to sell it.
        assert response.status_code == 402
        assert response.json()["error"]["code"] == "payment_required"

    async def test_download_is_401_when_anonymous(self, client, book_factory):
        book = await book_factory()
        assert (await client.get(f"/v1/books/{book.id}/download")).status_code == 401

    async def test_entitled_user_gets_a_signed_url(
        self, client, session, services, book_factory, as_user, monkeypatch
    ):
        book = await book_factory()
        await services["entitlements"].grant(
            session, user_id=READER_ID, book_id=book.id, source="purchase"
        )
        await session.commit()

        # Storage is stubbed: the assertion is that entitlement gating ran and a URL
        # was minted, not that boto3 works.
        from knowledgeos_core import deps as core_deps

        class _Storage:
            async def signed_download_url(self, key, *, expires_in, download_filename=None):
                return f"https://cdn.example/{key}?sig=abc"

        client._transport.app.dependency_overrides[core_deps.get_storage] = lambda: _Storage()
        try:
            as_user()
            response = await client.get(f"/v1/books/{book.id}/download")
            assert response.status_code == 200
            body = response.json()
            assert body["url"].startswith("https://cdn.example/")
            assert body["filename"].endswith(".pdf")
        finally:
            client._transport.app.dependency_overrides.pop(core_deps.get_storage, None)

    async def test_read_only_entitlement_cannot_download(self, session, services, book_factory):
        book = await book_factory()
        await services["entitlements"].grant(
            session,
            user_id=READER_ID,
            book_id=book.id,
            source="subscription",
            can_download=False,
        )
        await session.commit()

        # Reading is allowed...
        await services["entitlements"].require_read(session, user_id=READER_ID, book=book)
        # ...downloading is not.
        with pytest.raises(ForbiddenError):
            await services["entitlements"].require_download(session, user_id=READER_ID, book=book)

    async def test_expired_entitlement_denies_access(self, session, services, book_factory):
        book = await book_factory()
        _entitlement, _created = await services["entitlements"].grant(
            session,
            user_id=READER_ID,
            book_id=book.id,
            source="subscription",
            expires_at=datetime.now(UTC) - timedelta(days=1),
        )
        await session.commit()

        with pytest.raises(PaymentRequiredError):
            await services["entitlements"].require_read(session, user_id=READER_ID, book=book)

    async def test_free_books_are_readable_without_a_grant(self, session, services, book_factory):
        free = await book_factory(price_minor=0)
        access = await services["entitlements"].access(session, user_id=READER_ID, book=free)
        assert access.has_access is True

    async def test_entitlement_belongs_to_one_user_only(self, session, services, book_factory):
        book = await book_factory()
        await services["entitlements"].grant(
            session, user_id=READER_ID, book_id=book.id, source="purchase"
        )
        await session.commit()

        other = await services["entitlements"].access(session, user_id=OTHER_ID, book=book)
        assert other.has_access is False


class TestIdempotentGrants:
    async def test_repeated_grant_creates_one_row(self, session, services, book_factory):
        book = await book_factory()
        first, created_first = await services["entitlements"].grant(
            session, user_id=READER_ID, book_id=book.id, source="purchase", external_ref="order-1"
        )
        await session.commit()
        second, created_second = await services["entitlements"].grant(
            session, user_id=READER_ID, book_id=book.id, source="purchase", external_ref="order-1"
        )
        await session.commit()

        # Delivery is at-least-once, so a redelivered payment event must not grant
        # a second entitlement.
        assert first.id == second.id
        assert created_first is True
        assert created_second is False

    async def test_regrant_after_revocation_reinstates_the_same_row(
        self, session, services, book_factory
    ):
        book = await book_factory()
        entitlement, _created = await services["entitlements"].grant(
            session, user_id=READER_ID, book_id=book.id, source="purchase", external_ref="o1"
        )
        await session.commit()
        await services["entitlements"].revoke(session, entitlement=entitlement)
        await session.commit()

        # A refund that is later reversed must not leave two rows behind.
        again, created = await services["entitlements"].grant(
            session, user_id=READER_ID, book_id=book.id, source="purchase", external_ref="o1"
        )
        await session.commit()
        assert again.id == entitlement.id
        assert created is False
        assert again.revoked_at is None


class TestReviews:
    async def test_one_review_per_user_per_book(self, client, book_factory, as_user):
        book = await book_factory()
        as_user()
        payload = {"rating": 5, "title": "Great", "body": "Really enjoyed this one."}

        first = await client.post(f"/v1/books/{book.id}/reviews", json=payload)
        assert first.status_code == 201

        second = await client.post(f"/v1/books/{book.id}/reviews", json=payload)
        assert second.status_code == 409
        assert second.json()["error"]["code"] == "conflict"

    async def test_review_requires_authentication(self, client, book_factory):
        book = await book_factory()
        response = await client.post(
            f"/v1/books/{book.id}/reviews", json={"rating": 5, "body": "x" * 20}
        )
        assert response.status_code == 401

    async def test_rating_is_bounded(self, client, book_factory, as_user):
        book = await book_factory()
        as_user()
        response = await client.post(
            f"/v1/books/{book.id}/reviews", json={"rating": 9, "body": "x" * 20}
        )
        assert response.status_code == 422

    async def test_review_updates_the_denormalised_aggregate(self, session, services, book_factory):
        from schemas import ReviewCreate

        book = await book_factory()
        await services["reviews"].create(
            session,
            book=book,
            user_id=READER_ID,
            payload=ReviewCreate(rating=4, body="A perfectly good read, thank you."),
            verified_purchase=True,
        )
        await session.commit()
        # Aggregates live on the book so a catalogue page never AVG()s across reviews.
        assert book.rating_count == 1
        assert book.rating_average == pytest.approx(4.0)

    async def test_cannot_edit_another_users_review(self, session, services, book_factory):
        from schemas import ReviewCreate

        book = await book_factory()
        review = await services["reviews"].create(
            session,
            book=book,
            user_id=READER_ID,
            payload=ReviewCreate(rating=3, body="It was fine, nothing more."),
            verified_purchase=False,
        )
        await session.commit()

        # Ownership comes from the token. 403 rather than 404 here is deliberate:
        # the caller proved they are signed in and a review id is not a secret.
        with pytest.raises(ForbiddenError):
            await services["reviews"].get_own(session, review.id, user_id=OTHER_ID)

    async def test_cannot_vote_on_your_own_review(self, session, services, book_factory):
        from schemas import ReviewCreate

        book = await book_factory()
        review = await services["reviews"].create(
            session,
            book=book,
            user_id=READER_ID,
            payload=ReviewCreate(rating=5, body="I liked my own book a lot."),
            verified_purchase=False,
        )
        await session.commit()

        with pytest.raises(ForbiddenError):
            await services["reviews"].vote(
                session, review=review, user_id=READER_ID, is_helpful=True
            )


class TestOwnership:
    async def test_library_only_shows_your_own_books(
        self, client, session, services, book_factory, as_user
    ):
        mine = await book_factory(title="Mine")
        theirs = await book_factory(title="Theirs")
        await services["entitlements"].grant(
            session, user_id=READER_ID, book_id=mine.id, source="purchase"
        )
        await services["entitlements"].grant(
            session, user_id=OTHER_ID, book_id=theirs.id, source="purchase"
        )
        await session.commit()

        as_user()
        titles = [b["title"] for b in (await client.get("/v1/library")).json()["items"]]
        assert titles == ["Mine"]

    async def test_wishlist_requires_authentication(self, client):
        assert (await client.get("/v1/wishlist")).status_code == 401

    async def test_progress_requires_entitlement(self, client, book_factory, as_user):
        book = await book_factory()
        as_user()
        response = await client.put(
            f"/v1/reading-progress/{book.id}",
            json={"position": "epubcfi(/6/4)", "percent": 12.5},
        )
        assert response.status_code == 402


class TestAdminRoutes:
    async def test_admin_listing_requires_permission(self, client, as_user):
        as_user()  # plain reader
        assert (await client.get("/v1/admin/books")).status_code == 403

    async def test_admin_sees_drafts(self, client, book_factory, as_admin):
        await book_factory(title="Draft", status="draft", published_at=None)
        as_admin()
        body = (await client.get("/v1/admin/books")).json()
        assert "Draft" in [b["title"] for b in body["items"]]

    async def test_publish_transitions_status(self, client, book_factory, as_admin):
        book = await book_factory(status="draft", published_at=None)
        as_admin()
        response = await client.post(f"/v1/admin/books/{book.id}/publish")
        assert response.status_code == 200
        assert response.json()["status"] == "published"

    async def test_delete_is_soft(self, client, session, book_factory, as_admin):
        from sqlalchemy import select

        from models import Book

        book = await book_factory()
        as_admin()
        assert (await client.delete(f"/v1/admin/books/{book.id}")).status_code == 204

        # The row survives: purchase history and entitlements reference it.
        # A scalar select rather than session.get(), because this fixture's identity
        # map still holds the pre-delete object from a different session.
        deleted_at = (
            await session.execute(select(Book.deleted_at).where(Book.id == book.id))
        ).scalar_one()
        assert deleted_at is not None


class TestInternalRoutes:
    async def test_internal_requires_a_signature(self, client):
        response = await client.post("/internal/books/batch", json={"book_ids": []})
        assert response.status_code == 401

    async def test_internal_batch_with_a_valid_caller(self, client, book_factory, as_internal):
        book = await book_factory()
        as_internal()
        response = await client.post("/internal/books/batch", json={"book_ids": [str(book.id)]})
        assert response.status_code == 200
        assert [b["id"] for b in response.json()["items"]] == [str(book.id)]

    async def test_internal_entitlement_check_reports_what_a_user_owns(
        self, client, book_factory, as_internal, session
    ):
        """The payment service prices a cart with this, so a customer is not charged
        twice for a file they already hold."""
        from services.entitlements import EntitlementService
        from settings import settings as service_settings

        owned = await book_factory()
        other = await book_factory()
        await EntitlementService(service_settings).grant(
            session, user_id=READER_ID, book_id=owned.id, source="purchase"
        )
        await session.commit()

        as_internal()
        response = await client.post(
            "/internal/entitlements/check",
            json={"user_id": str(READER_ID), "book_ids": [str(owned.id), str(other.id)]},
        )
        assert response.status_code == 200
        assert response.json()["owned_book_ids"] == [str(owned.id)]


class TestTaxonomyListings:
    """Regression: these three routers called a service signature that does not
    exist, so every list endpoint was a 500. Nothing covered them."""

    async def test_authors_are_listable(self, client, session):
        from models import Author

        session.add(Author(name="Ada Lovelace", slug="ada-lovelace"))
        await session.commit()

        response = await client.get("/v1/authors")
        assert response.status_code == 200
        body = response.json()
        assert body["items"][0]["name"] == "Ada Lovelace"
        assert body["meta"]["total"] == 1

    async def test_authors_can_be_searched(self, client, session):
        from models import Author

        session.add_all(
            [Author(name="Ada Lovelace", slug="ada"), Author(name="Alan Turing", slug="alan")]
        )
        await session.commit()

        body = (await client.get("/v1/authors?q=Turing")).json()
        assert [author["name"] for author in body["items"]] == ["Alan Turing"]

    async def test_publishers_are_listable(self, client, session):
        from models import Publisher

        session.add(Publisher(name="KnowledgeOS Press", slug="kos-press"))
        await session.commit()

        response = await client.get("/v1/publishers")
        assert response.status_code == 200
        assert response.json()["meta"]["total"] == 1

    async def test_categories_are_listable(self, client, session):
        from models import Category

        session.add(Category(name="Fiction", slug="fiction"))
        await session.commit()

        response = await client.get("/v1/categories")
        assert response.status_code == 200
        assert response.json()["items"][0]["slug"] == "fiction"

    async def test_listings_paginate(self, client, session):
        from models import Author

        session.add_all([Author(name=f"Author {n}", slug=f"a{n}") for n in range(5)])
        await session.commit()

        first = (await client.get("/v1/authors?page=1&limit=2")).json()
        assert len(first["items"]) == 2
        assert first["meta"]["pages"] == 3

        last = (await client.get("/v1/authors?page=3&limit=2")).json()
        assert len(last["items"]) == 1


class TestInternalListings:
    """The search service reconciles its index by walking these."""

    async def test_the_book_listing_returns_a_cursor(self, client, book_factory, as_internal):
        """A response without one stops the reconciler after a single page, and the
        symptom is an index that only ever holds the newest few hundred books."""
        for _ in range(3):
            await book_factory()
        as_internal()

        first = (await client.get("/internal/books?limit=2")).json()
        assert len(first["items"]) == 2
        assert first["next_cursor"]

        second = (await client.get(f"/internal/books?limit=2&cursor={first['next_cursor']}")).json()
        assert len(second["items"]) == 1
        assert second["next_cursor"] is None

    async def test_the_book_listing_carries_the_fields_the_indexer_needs(
        self, client, book_factory, as_internal
    ):
        """A card-sized projection is missing the description, tags and formats the
        search document is built from."""
        await book_factory(description="Full text here.")
        as_internal()

        item = (await client.get("/internal/books")).json()["items"][0]
        assert item["description"] == "Full text here."
        assert "effective_price_minor" in item
        assert "ai_tags" in item

    async def test_authors_and_categories_are_walkable(self, client, session, as_internal):
        from models import Author, Category

        session.add(Author(name="Ada Lovelace", slug="ada-lovelace"))
        session.add(Category(name="Fiction", slug="fiction"))
        await session.commit()
        as_internal()

        authors = (await client.get("/internal/authors")).json()
        categories = (await client.get("/internal/categories")).json()
        assert authors["items"][0]["slug"] == "ada-lovelace"
        assert authors["next_cursor"] is None  # single page
        assert categories["items"][0]["slug"] == "fiction"


class TestUploads:
    async def test_an_admin_can_mint_an_upload_target(self, client, as_admin):
        """Regression: `category` is a required enum field, so use_enum_values has
        already turned it into a plain string — reading `.value` off it 500s."""
        as_admin()
        response = await client.post(
            "/v1/admin/books/uploads",
            json={
                "category": "book",
                "filename": "manual.pdf",
                "content_type": "application/pdf",
            },
        )
        assert response.status_code == 200
        assert response.json()["url"]


class TestPlatformContract:
    async def test_health_and_metrics(self, client):
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/metrics")).status_code == 200

    async def test_errors_use_the_platform_envelope(self, client):
        error = (await client.get("/v1/books/nope")).json()["error"]
        assert set(error) >= {"code", "message"}


class TestModerationQueue:
    """The per-book review listing cannot serve a moderator: they do not know which
    book has something pending, which is the entire question."""

    async def test_the_queue_spans_the_whole_catalogue(
        self, client, session, book_factory, as_admin, as_user, settings, monkeypatch
    ):
        monkeypatch.setattr(settings, "reviews_require_moderation", True)
        first, second = await book_factory(slug="one"), await book_factory(slug="two")

        as_user(READER_ID)
        await client.post(f"/v1/books/{first.id}/reviews", json={"rating": 5, "body": "Good."})
        as_user(OTHER_ID)
        await client.post(f"/v1/books/{second.id}/reviews", json={"rating": 2, "body": "Bad."})

        as_admin()
        response = await client.get("/v1/admin/moderation/reviews")
        body = response.json()

        assert response.status_code == 200
        assert body["total"] == 2
        # Two different books in one queue — the thing the per-book route cannot do.
        assert len({row["book_id"] for row in body["items"]}) == 2

    async def test_the_queue_is_oldest_first(
        self, client, session, book_factory, as_admin, as_user, settings, monkeypatch
    ):
        """A queue worked newest-first leaves its oldest items forever, and those are
        exactly the ones a customer is waiting on."""
        monkeypatch.setattr(settings, "reviews_require_moderation", True)
        book = await book_factory(slug="queued")

        as_user(READER_ID)
        await client.post(f"/v1/books/{book.id}/reviews", json={"rating": 5, "body": "First."})
        as_user(OTHER_ID)
        await client.post(f"/v1/books/{book.id}/reviews", json={"rating": 1, "body": "Second."})

        as_admin()
        items = (await client.get("/v1/admin/moderation/reviews")).json()["items"]
        assert items[0]["body"] == "First."

    async def test_it_reports_a_real_total(
        self, client, book_factory, as_admin, as_user, settings, monkeypatch
    ):
        """ "37 waiting" is the number that decides whether someone starts, and an
        infinite scroll cannot show it."""
        monkeypatch.setattr(settings, "reviews_require_moderation", True)
        book = await book_factory(slug="counted")

        as_user(READER_ID)
        await client.post(f"/v1/books/{book.id}/reviews", json={"rating": 4, "body": "Fine."})

        as_admin()
        body = (await client.get("/v1/admin/moderation/reviews", params={"limit": 1})).json()
        assert body["total"] == 1
        assert body["limit"] == 1

    async def test_it_defaults_to_pending_only(
        self, client, book_factory, as_admin, as_user, settings, monkeypatch
    ):
        monkeypatch.setattr(settings, "reviews_require_moderation", False)
        book = await book_factory(slug="approved-only")

        as_user(READER_ID)
        await client.post(f"/v1/books/{book.id}/reviews", json={"rating": 5, "body": "Auto."})

        as_admin()
        pending = (await client.get("/v1/admin/moderation/reviews")).json()
        approved = (
            await client.get("/v1/admin/moderation/reviews", params={"status": "approved"})
        ).json()

        assert pending["total"] == 0
        assert approved["total"] == 1

    async def test_counts_include_every_state(
        self, client, book_factory, as_admin, as_user, settings, monkeypatch
    ):
        """The UI should never special-case a missing key and render a blank where a
        zero belongs."""
        monkeypatch.setattr(settings, "reviews_require_moderation", True)
        book = await book_factory(slug="counts")

        as_user(READER_ID)
        await client.post(f"/v1/books/{book.id}/reviews", json={"rating": 3, "body": "Meh."})

        as_admin()
        counts = (await client.get("/v1/admin/moderation/reviews/counts")).json()

        assert set(counts) == {"pending", "approved", "rejected", "flagged"}
        assert counts["pending"] == 1
        assert counts["approved"] == 0

    async def test_the_queue_needs_the_moderate_permission(self, client, as_admin, as_user):
        as_user(READER_ID, permissions=["books:read"])
        assert (await client.get("/v1/admin/moderation/reviews")).status_code == 403
