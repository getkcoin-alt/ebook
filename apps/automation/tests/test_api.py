"""The HTTP surface: operator routes, internal routes, imports and events."""

from __future__ import annotations

import uuid

import pytest

from tests.conftest import BOOK_ID, SOURCE_KEY

JOB_BODY = {
    "book_id": str(BOOK_ID),
    "source_key": SOURCE_KEY,
    "original_filename": "deep-work.pdf",
}


class TestJobRoutes:
    async def test_queueing_a_job_returns_its_full_stage_history(self, client, as_admin, services):
        as_admin()
        response = await client.post("/v1/automation/jobs", json=JOB_BODY)

        assert response.status_code == 201
        body = response.json()
        assert body["status"] == "queued"
        assert len(body["stages"]) == 13
        # The API hands the job off rather than running it inside the request.
        assert services["dispatch"].calls == [uuid.UUID(body["id"])]

    async def test_a_second_job_for_the_same_file_is_a_conflict(self, client, as_admin):
        as_admin()
        await client.post("/v1/automation/jobs", json=JOB_BODY)
        again = await client.post("/v1/automation/jobs", json=JOB_BODY)

        assert again.status_code == 409

    async def test_a_traversal_source_key_is_rejected_by_validation(self, client, as_admin):
        """The key becomes part of a derived artefact key; `..` is how one escapes
        its prefix."""
        as_admin()
        response = await client.post(
            "/v1/automation/jobs", json={**JOB_BODY, "source_key": "../../etc/passwd"}
        )
        assert response.status_code == 422

    async def test_reading_a_job_needs_only_the_read_permission(self, client, as_admin):
        as_admin()
        created = (await client.post("/v1/automation/jobs", json=JOB_BODY)).json()

        as_admin(permissions=["automation:read"])
        response = await client.get(f"/v1/automation/jobs/{created['id']}")
        assert response.status_code == 200

    async def test_queueing_a_job_needs_the_run_permission(self, client, as_admin):
        as_admin(permissions=["automation:read"])
        assert (await client.post("/v1/automation/jobs", json=JOB_BODY)).status_code == 403

    async def test_an_unknown_job_is_a_404(self, client, as_admin):
        as_admin()
        response = await client.get(f"/v1/automation/jobs/{uuid.uuid4()}")
        assert response.status_code == 404

    async def test_jobs_can_be_filtered_by_status_and_book(self, client, as_admin):
        as_admin()
        await client.post("/v1/automation/jobs", json=JOB_BODY)

        matching = await client.get("/v1/automation/jobs", params={"status": "queued"})
        assert matching.json()["total"] == 1

        other = await client.get("/v1/automation/jobs", params={"book_id": str(uuid.uuid4())})
        assert other.json()["total"] == 0

    async def test_running_a_job_inline_reports_what_happened(self, client, as_admin):
        as_admin()
        created = (await client.post("/v1/automation/jobs", json=JOB_BODY)).json()

        response = await client.post(f"/v1/automation/jobs/{created['id']}/run")
        assert response.status_code == 200
        assert response.json()["status"] == "succeeded"
        assert "stages completed" in response.json()["message"]

    async def test_a_finished_job_cannot_be_run_again(self, client, as_admin):
        as_admin()
        created = (await client.post("/v1/automation/jobs", json=JOB_BODY)).json()
        await client.post(f"/v1/automation/jobs/{created['id']}/run")

        again = await client.post(f"/v1/automation/jobs/{created['id']}/run")
        assert again.status_code == 409

    async def test_cancelling_stops_a_job_being_picked_up(self, client, as_admin):
        as_admin()
        created = (await client.post("/v1/automation/jobs", json=JOB_BODY)).json()

        response = await client.post(f"/v1/automation/jobs/{created['id']}/cancel")
        assert response.json()["status"] == "cancelled"

    async def test_retry_requeues_and_redispatches(self, client, as_admin, services):
        as_admin()
        created = (await client.post("/v1/automation/jobs", json=JOB_BODY)).json()
        await client.post(f"/v1/automation/jobs/{created['id']}/run")
        services["dispatch"].calls.clear()

        response = await client.post(
            f"/v1/automation/jobs/{created['id']}/retry",
            json={"from_stage": "thumbnail", "reset_attempts": True},
        )
        assert response.status_code == 200
        assert response.json()["status"] == "queued"
        assert services["dispatch"].calls == [uuid.UUID(created["id"])]

    async def test_stats_report_per_stage_failures(self, client, as_admin):
        as_admin()
        response = await client.get("/v1/automation/stats", params={"days": 7})

        assert response.status_code == 200
        assert response.json()["window_days"] == 7
        assert response.json()["failures_by_stage"] == {}


class TestInternalRoutes:
    async def test_a_sibling_service_can_queue_a_job(self, client, as_internal):
        as_internal("books")
        response = await client.post("/internal/jobs", json=JOB_BODY)

        assert response.status_code == 200
        assert response.json()["source"] == "upload"

    async def test_internal_routes_are_closed_without_a_signature(self, client):
        """Private-network reachability is not authorisation."""
        response = await client.post("/internal/jobs", json=JOB_BODY)
        assert response.status_code in (401, 403)

    async def test_the_drain_sweep_runs_due_jobs(self, client, as_internal, as_admin):
        as_admin()
        await client.post("/v1/automation/jobs", json=JOB_BODY)

        as_internal("workers")
        response = await client.post("/internal/maintenance/drain", params={"limit": 5})
        assert response.json()["retried"] == 1

    async def test_the_stale_sweep_reports_what_it_rescued(self, client, as_internal):
        as_internal("workers")
        response = await client.post("/internal/maintenance/requeue-stale")
        assert response.json()["requeued_stale"] == 0

    async def test_the_prune_sweep_reports_what_it_dropped(self, client, as_internal):
        as_internal("workers")
        response = await client.post("/internal/maintenance/prune")
        assert response.json()["pruned_jobs"] == 0

    async def test_jobs_for_a_book_are_listable(self, client, as_internal, as_admin):
        as_admin()
        await client.post("/v1/automation/jobs", json=JOB_BODY)

        as_internal("books")
        response = await client.get("/internal/jobs", params={"book_id": str(BOOK_ID)})
        assert len(response.json()) == 1


class TestImports:
    async def test_a_dry_run_writes_nothing(self, client, as_admin, clients):
        as_admin()
        response = await client.post(
            "/v1/automation/imports",
            json={
                "filename": "spring.csv",
                "dry_run": True,
                "rows": [
                    {"title": "Deep Work", "price_minor": 49900, "line": 2},
                    {"title": "Atomic Habits", "price_minor": 39900, "line": 3},
                ],
            },
        )

        assert response.status_code == 201
        assert response.json()["succeeded"] == 2
        assert clients.created == []

    async def test_a_real_import_creates_books_and_queues_jobs(
        self, client, as_admin, clients, services
    ):
        as_admin()
        response = await client.post(
            "/v1/automation/imports",
            json={
                "dry_run": False,
                "rows": [
                    {
                        "title": "Deep Work",
                        "price_minor": 49900,
                        "source_key": SOURCE_KEY,
                        "line": 2,
                    }
                ],
            },
        )

        assert response.json()["status"] == "completed"
        assert len(clients.created) == 1
        assert clients.created[0]["slug"] == "deep-work"
        assert len(services["dispatch"].calls) == 1

    async def test_a_row_without_a_file_creates_a_book_but_no_job(
        self, client, as_admin, clients, services
    ):
        """A metadata-only backfill for books whose files arrive later."""
        as_admin()
        response = await client.post(
            "/v1/automation/imports",
            json={"dry_run": False, "rows": [{"title": "Deep Work", "price_minor": 49900}]},
        )

        assert response.json()["succeeded"] == 1
        assert clients.created
        assert services["dispatch"].calls == []

    async def test_one_bad_row_does_not_abort_the_others(self, client, as_admin, clients):
        """A single typo on line 287 must not send an editor back to the start."""
        as_admin()
        response = await client.post(
            "/v1/automation/imports",
            json={
                "dry_run": False,
                "rows": [
                    {"title": "Good One", "price_minor": 49900, "line": 2},
                    {"title": "Bad ISBN", "isbn13": "123", "price_minor": 49900, "line": 3},
                    {"title": "Another Good", "price_minor": 39900, "line": 4},
                ],
            },
        )

        body = response.json()
        assert body["status"] == "partial"
        assert body["succeeded"] == 2
        assert body["failed"] == 1
        assert body["errors"][0]["line"] == 3

    async def test_a_sub_unit_price_is_caught(self, client, as_admin):
        """A non-zero price below one whole currency unit is an unconverted value."""
        as_admin()
        response = await client.post(
            "/v1/automation/imports",
            json={"dry_run": True, "rows": [{"title": "Cheap", "price_minor": 40, "line": 5}]},
        )

        assert response.json()["failed"] == 1
        assert "whole currency unit" in response.json()["errors"][0]["error"]

    async def test_a_free_book_is_allowed(self, client, as_admin):
        """Plenty of catalogues carry free titles; zero is not a conversion mistake."""
        as_admin()
        response = await client.post(
            "/v1/automation/imports",
            json={"dry_run": True, "rows": [{"title": "Free Read", "price_minor": 0}]},
        )
        assert response.json()["succeeded"] == 1

    async def test_a_duplicate_title_within_one_import_is_reported(self, client, as_admin):
        as_admin()
        response = await client.post(
            "/v1/automation/imports",
            json={
                "dry_run": True,
                "rows": [
                    {"title": "Deep Work", "price_minor": 49900, "line": 2},
                    {"title": "Deep  Work!", "price_minor": 49900, "line": 3},
                ],
            },
        )

        assert response.json()["failed"] == 1
        assert "duplicate" in response.json()["errors"][0]["error"]

    async def test_an_import_where_every_row_fails_is_not_reported_as_completed(
        self, client, as_admin, clients
    ):
        as_admin()
        clients.create_fails = True
        response = await client.post(
            "/v1/automation/imports",
            json={"dry_run": False, "rows": [{"title": "Deep Work", "price_minor": 49900}]},
        )

        assert response.json()["status"] == "failed"

    async def test_imports_are_listable_and_readable(self, client, as_admin):
        as_admin()
        created = (
            await client.post(
                "/v1/automation/imports",
                json={"dry_run": True, "rows": [{"title": "Deep Work", "price_minor": 49900}]},
            )
        ).json()

        listing = await client.get("/v1/automation/imports")
        assert listing.json()["total"] == 1

        detail = await client.get(f"/v1/automation/imports/{created['id']}")
        assert detail.json()["id"] == created["id"]

    async def test_an_unknown_import_is_a_404(self, client, as_admin):
        as_admin()
        assert (await client.get(f"/v1/automation/imports/{uuid.uuid4()}")).status_code == 404


class TestSlugify:
    @pytest.mark.parametrize(
        ("title", "expected"),
        [
            ("Deep Work", "deep-work"),
            ("Café Society", "cafe-society"),
            ("  Spaces  Everywhere  ", "spaces-everywhere"),
            ("C++ for Everyone", "c-for-everyone"),
        ],
    )
    def test_titles_become_readable_slugs(self, title, expected):
        from services import slugify

        assert slugify(title) == expected

    def test_a_title_with_no_ascii_still_gets_a_slug(self):
        """An empty slug is a 422 from the books service on an otherwise valid row."""
        from services import slugify

        slug = slugify("日本語")
        assert slug and slug.startswith("book-")


class TestEventConsumption:
    async def test_a_book_created_event_with_a_file_queues_a_job(self, session, jobs, settings):
        from knowledgeos_core import Event, EventType
        from services import AutomationEventHandler

        handler = AutomationEventHandler(settings, jobs)
        event = Event(
            type=EventType.BOOK_CREATED,
            payload={"book_id": str(BOOK_ID), "source_key": SOURCE_KEY},
        )
        await handler.handle(session, event)
        await session.commit()

        rows, total = await jobs.list_jobs(session)
        assert total == 1
        assert rows[0].source == "event"

    async def test_a_redelivered_event_does_not_queue_a_second_job(self, session, jobs, settings):
        """Redis Streams deliver at least once. Two runs would both write derived
        artefacts and both try to publish."""
        from knowledgeos_core import Event, EventType
        from services import AutomationEventHandler

        handler = AutomationEventHandler(settings, jobs)
        event = Event(
            type=EventType.BOOK_CREATED,
            payload={"book_id": str(BOOK_ID), "source_key": SOURCE_KEY},
        )
        await handler.handle(session, event)
        await session.commit()
        await handler.handle(session, event)
        await session.commit()

        _rows, total = await jobs.list_jobs(session)
        assert total == 1

    async def test_a_book_created_without_a_file_is_ignored(self, session, jobs, settings):
        """A book created through the admin UI. There is nothing to process."""
        from knowledgeos_core import Event, EventType
        from services import AutomationEventHandler

        handler = AutomationEventHandler(settings, jobs)
        await handler.handle(
            session, Event(type=EventType.BOOK_CREATED, payload={"book_id": str(BOOK_ID)})
        )
        await session.commit()

        _rows, total = await jobs.list_jobs(session)
        assert total == 0

    async def test_two_different_events_for_one_file_do_not_both_start_a_job(
        self, session, jobs, settings
    ):
        """The ledger cannot catch this — the events are genuinely different. The
        partial unique index on the job table is what does."""
        from knowledgeos_core import Event, EventType
        from services import AutomationEventHandler

        handler = AutomationEventHandler(settings, jobs)
        payload = {"book_id": str(BOOK_ID), "source_key": SOURCE_KEY}
        await handler.handle(session, Event(type=EventType.BOOK_CREATED, payload=payload))
        await session.commit()
        await handler.handle(session, Event(type=EventType.BOOK_CREATED, payload=payload))
        await session.commit()

        _rows, total = await jobs.list_jobs(session)
        assert total == 1


class TestCatalogueUpdateShape:
    def test_only_produced_fields_are_sent(self):
        """Sending nulls for the rest would clear an editor's hand-written description
        the moment an AI stage was skipped."""
        from services.stages import build_catalogue_update

        patch = build_catalogue_update({"document": {"page_count": 300}})

        assert patch == {"page_count": 300}
        assert "description" not in patch

    def test_a_malformed_isbn_is_dropped_rather_than_failing_the_whole_patch(self):
        from services.stages import build_catalogue_update

        patch = build_catalogue_update({"document": {"isbn13": "97814555", "page_count": 10}})

        assert "isbn13" not in patch
        assert patch["page_count"] == 10

    def test_fields_are_truncated_to_the_catalogue_column_widths(self):
        """A model told "at most 60 characters" produces 63 often enough to matter,
        and a 422 would throw away a whole batch of usable output."""
        from services.stages import build_catalogue_update

        patch = build_catalogue_update({"seo": {"meta_title": "x" * 400}})
        assert len(patch["meta_title"]) == 255


class TestAiResponseParsing:
    def test_the_batch_response_maps_onto_catalogue_fields(self):
        from services.clients import _parse_ai_batch

        output = _parse_ai_batch(
            {
                "results": {
                    "description": {"content": "  A book.  "},
                    "seo": {"data": {"meta_title": "T", "keywords": ["a", "b"]}},
                    "tags": {"data": {"tags": ["Focus", "focus", "Deep-Work"]}},
                },
                "failed": ["summary"],
                "total_cost_usd": 0.03,
            }
        )

        assert output.description == "A book."
        assert output.meta_title == "T"
        # Lower-cased and de-duplicated: tags are a facet, and "Focus" and "focus"
        # would otherwise be two of them.
        assert output.tags == ["focus", "deep-work"]
        assert output.failed == ["summary"]
        assert output.cost_usd == 0.03

    def test_a_response_with_nothing_usable_is_empty_not_partial(self):
        from services.clients import _parse_ai_batch

        assert _parse_ai_batch({"results": {}}).empty is True

    def test_non_string_list_members_are_dropped(self):
        from services.clients import _parse_ai_batch

        output = _parse_ai_batch({"results": {"tags": {"data": {"tags": ["ok", 5, None]}}}})
        assert output.tags == ["ok"]
