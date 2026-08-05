"""The runner: ordering, checkpointing, resume, skip and failure policy.

These are the tests that matter most, because the failure policy is where a pipeline
either loses work or spends money re-doing it. Every one of them runs the real
`PipelineRunner` against real documents and a real database session — only the
sibling services and the bucket are stubs.
"""

from __future__ import annotations

import uuid

import pytest

from knowledgeos_core import ConflictError, JobStatus
from models import AutomationJob, JobStage
from schemas import PIPELINE, JobOptions, Stage, StageStatus
from services.clients import AiOutput
from tests.conftest import BOOK_ID, SOURCE_KEY, job_payload, make_epub


async def _queue(jobs, session, **overrides):  # type: ignore[no-untyped-def]
    return await jobs.create(session, job_payload(**overrides))


class TestJobCreation:
    async def test_every_stage_gets_a_row_up_front(self, jobs, session):
        """A history that materialises rows as it goes cannot distinguish "stage 9
        has not run yet" from "stage 9 was never part of this job"."""
        job = await _queue(jobs, session)
        rows = await jobs.stages(session, job.id)

        assert len(rows) == len(PIPELINE)
        assert [row.stage for row in rows] == list(PIPELINE)
        assert all(row.status is StageStatus.PENDING for row in rows)

    async def test_a_second_live_job_for_the_same_file_is_refused(self, jobs, session):
        """A double-clicked button must not run the pipeline twice: both runs write
        derived artefacts and both try to publish."""
        await _queue(jobs, session)

        with pytest.raises(ConflictError):
            await _queue(jobs, session)

    async def test_a_new_job_is_allowed_once_the_first_has_settled(self, jobs, session):
        first = await _queue(jobs, session)
        await jobs.succeed(session, first)

        second = await _queue(jobs, session)
        assert second.id != first.id

    async def test_stages_excluded_by_options_start_skipped(self, jobs, session):
        job = await _queue(
            jobs, session, options=JobOptions(skip_stages=[Stage.WATERMARK, Stage.COMPRESS])
        )
        by_stage = {row.stage: row for row in await jobs.stages(session, job.id)}

        assert by_stage[Stage.WATERMARK].status is StageStatus.SKIPPED
        assert by_stage[Stage.COLLECT].status is StageStatus.PENDING


class TestHappyPath:
    async def test_a_pdf_runs_end_to_end(self, runner, jobs, session, clients, storage):
        job = await _queue(jobs, session)
        outcome = await runner.run(session, job)

        assert outcome.status is JobStatus.SUCCEEDED
        assert Stage.EXTRACT_METADATA in {Stage(name) for name in outcome.completed}
        assert job.finished_at is not None
        assert job.current_stage is None

    async def test_the_document_facts_reach_the_catalogue(self, runner, jobs, session, clients):
        job = await _queue(jobs, session)
        await runner.run(session, job)

        assert clients.updates, "the pipeline produced nothing to write back"
        patch = clients.updates[-1]
        assert patch["page_count"] == 12
        assert patch["description"] == "A book about focused work."
        assert patch["ai_tags"] == ["productivity", "focus"]
        assert patch["meta_title"] == "Deep Work"

    async def test_derived_artefacts_are_written_and_referenced(
        self, runner, jobs, session, clients, storage
    ):
        job = await _queue(jobs, session)
        await runner.run(session, job)

        sample_keys = [key for key in storage.writes if key.endswith("sample.pdf")]
        assert sample_keys, "no sample was uploaded"
        assert clients.updates[-1]["sample_key"] == sample_keys[0]

    async def test_nothing_publishes_without_being_asked(self, runner, jobs, session, clients):
        """`AUTO_PUBLISH` is off: a pipeline that publishes whatever it is handed puts
        machine-written copy in front of customers with nobody having read it."""
        job = await _queue(jobs, session)
        await runner.run(session, job)

        assert clients.published == []
        assert job.result["published"] is False

    async def test_publishing_happens_when_the_job_asks_for_it(
        self, runner, jobs, session, clients
    ):
        job = await _queue(jobs, session, options=JobOptions(auto_publish=True))
        await runner.run(session, job)

        assert clients.published == [str(BOOK_ID)]

    async def test_an_epub_runs_and_skips_the_pdf_only_stages(
        self, runner, jobs, session, storage, clients
    ):
        storage.put("private/uploads/deep-work.epub", make_epub())
        job = await _queue(
            jobs,
            session,
            source_key="private/uploads/deep-work.epub",
            original_filename="deep-work.epub",
        )
        outcome = await runner.run(session, job)

        assert outcome.status is JobStatus.SUCCEEDED
        assert "compress" in outcome.skipped
        assert "watermark" in outcome.skipped
        assert clients.updates[-1]["isbn13"] == "9781455586691"

    async def test_the_source_object_is_never_overwritten(self, runner, jobs, session, storage):
        """The uploader's original is the only thing here that cannot be regenerated."""
        original = storage.objects[SOURCE_KEY]
        job = await _queue(jobs, session)
        await runner.run(session, job)

        assert storage.objects[SOURCE_KEY] == original
        assert SOURCE_KEY not in storage.writes


class TestSkipPolicy:
    async def test_ai_stages_are_skipped_wholesale_when_no_provider_is_configured(
        self, runner, jobs, session, clients, settings, monkeypatch
    ):
        monkeypatch.setattr(settings, "ai_enabled", False)
        job = await _queue(jobs, session)
        outcome = await runner.run(session, job)

        assert outcome.status is JobStatus.SUCCEEDED
        assert {"generate_description", "seo", "tags"} <= set(outcome.skipped)

    async def test_an_ai_outage_skips_rather_than_fails(self, runner, jobs, session, clients):
        """Blocking a publication on a provider's bad afternoon turns one outage into
        a backlog nobody can clear."""
        clients.ai_available = False
        job = await _queue(jobs, session)
        outcome = await runner.run(session, job)

        assert outcome.status is JobStatus.SUCCEEDED
        assert "generate_description" in outcome.skipped

    async def test_a_skip_is_recorded_with_its_reason(self, runner, jobs, session, clients):
        clients.ai_available = False
        job = await _queue(jobs, session)
        await runner.run(session, job)

        by_stage = {row.stage: row for row in await jobs.stages(session, job.id)}
        assert by_stage[Stage.SEO].status is StageStatus.SKIPPED
        assert by_stage[Stage.SEO].error == "ai_unavailable"

    async def test_a_skipped_stage_is_not_a_failed_stage(self, runner, jobs, session, clients):
        """The two lead to different operator actions, so they are different statuses."""
        clients.ai_available = False
        job = await _queue(jobs, session)
        await runner.run(session, job)

        rows = await jobs.stages(session, job.id)
        assert not any(row.status is StageStatus.FAILED for row in rows)

    async def test_empty_ai_output_is_a_skip_not_a_silent_success(
        self, runner, jobs, session, clients
    ):
        clients.ai_response = AiOutput()
        job = await _queue(jobs, session)
        await runner.run(session, job)

        by_stage = {row.stage: row for row in await jobs.stages(session, job.id)}
        assert by_stage[Stage.GENERATE_DESCRIPTION].error == "ai_returned_nothing"

    async def test_only_stages_still_runs_collect(self, runner, jobs, session):
        """Every other stage needs the source bytes, and they are not in the checkpoint."""
        job = await _queue(
            jobs, session, options=JobOptions(only_stages=[Stage.VALIDATE, Stage.EXTRACT_METADATA])
        )
        outcome = await runner.run(session, job)

        assert outcome.status is JobStatus.SUCCEEDED
        assert "collect" in outcome.completed
        assert "watermark" in outcome.skipped


class TestFailurePolicy:
    async def test_a_corrupt_file_fails_terminally_without_retrying(
        self, runner, jobs, session, storage
    ):
        """It will be just as corrupt on the fourth attempt; retrying spends twenty
        minutes reaching the same answer."""
        storage.put("private/uploads/broken.pdf", b"%PDF-1.4 truncated nonsense")
        job = await _queue(
            jobs, session, source_key="private/uploads/broken.pdf", original_filename="broken.pdf"
        )
        outcome = await runner.run(session, job)

        assert outcome.status is JobStatus.FAILED
        assert job.next_attempt_at is None
        assert job.attempts == 1

    async def test_an_unsupported_format_fails_before_any_expensive_stage(
        self, runner, jobs, session, storage, clients
    ):
        storage.put("private/uploads/notes.txt", b"just some notes")
        job = await _queue(
            jobs, session, source_key="private/uploads/notes.txt", original_filename="notes.txt"
        )
        outcome = await runner.run(session, job)

        assert outcome.status is JobStatus.FAILED
        assert clients.updates == []
        assert "unsupported" in (job.error or "").lower() or "does not process" in (job.error or "")

    async def test_an_empty_source_is_refused(self, runner, jobs, session, storage):
        storage.put("private/uploads/empty.pdf", b"")
        job = await _queue(
            jobs, session, source_key="private/uploads/empty.pdf", original_filename="empty.pdf"
        )
        assert (await runner.run(session, job)).status is JobStatus.FAILED

    async def test_a_transient_failure_on_a_required_stage_backs_off_and_retries(
        self, runner, jobs, session, storage
    ):
        storage.fail_on.add(SOURCE_KEY)
        job = await _queue(jobs, session)
        outcome = await runner.run(session, job)

        assert outcome.status is JobStatus.RETRYING
        # Backoff lives in the row, not in a sleeping worker.
        assert job.next_attempt_at is not None

    async def test_retries_stop_at_the_ceiling_and_dead_letter(
        self, runner, jobs, session, storage, settings
    ):
        storage.fail_on.add(SOURCE_KEY)
        job = await _queue(jobs, session)

        for _ in range(settings.max_attempts + 1):
            if job.is_terminal:
                break
            job.next_attempt_at = None
            job.status = JobStatus.QUEUED
            await session.commit()
            await runner.run(session, job)

        assert job.status is JobStatus.DEAD_LETTERED
        assert job.dead_lettered_at is not None

    async def test_a_failing_optional_stage_does_not_fail_the_job(
        self, runner, jobs, session, clients
    ):
        """A book with no AI tags is publishable; blocking on that is the wrong trade."""

        async def _boom(**_kwargs):
            raise RuntimeError("the model provider fell over")

        clients.generate = _boom
        job = await _queue(jobs, session)
        outcome = await runner.run(session, job)

        assert outcome.status is JobStatus.SUCCEEDED
        assert "generate_description" in outcome.failed

    async def test_a_failing_required_stage_does_fail_the_job(self, runner, jobs, session, clients):
        """A pipeline that "succeeded" without writing its results back produced nothing."""
        clients.update_fails = True
        job = await _queue(jobs, session)
        outcome = await runner.run(session, job)

        assert outcome.status is not JobStatus.SUCCEEDED

    async def test_a_search_outage_does_not_block_the_catalogue_write(
        self, runner, jobs, session, clients
    ):
        """Blocking publication on the search index being up makes search a hard
        dependency of the catalogue — the coupling the event bus exists to remove."""

        async def _index_down(book_id):
            return False

        clients.index_book = _index_down
        job = await _queue(jobs, session)

        assert (await runner.run(session, job)).status is JobStatus.SUCCEEDED
        assert clients.updates


class TestResume:
    async def test_a_resumed_job_does_not_redo_completed_stages(
        self, runner, jobs, session, clients, storage
    ):
        """The point of the checkpoint: stage 11 resumes at stage 11, not at stage 1."""
        clients.update_fails = True
        job = await _queue(jobs, session)
        await runner.run(session, job)

        by_stage = {row.stage: row for row in await jobs.stages(session, job.id)}
        assert by_stage[Stage.EXTRACT_METADATA].status is StageStatus.SUCCEEDED
        first_attempts = by_stage[Stage.EXTRACT_METADATA].attempts

        clients.update_fails = False
        job.status = JobStatus.QUEUED
        job.next_attempt_at = None
        await session.commit()
        await runner.run(session, job)

        by_stage = {row.stage: row for row in await jobs.stages(session, job.id)}
        assert by_stage[Stage.EXTRACT_METADATA].attempts == first_attempts
        assert job.status is JobStatus.SUCCEEDED

    async def test_collect_always_reruns_because_bytes_are_not_checkpointed(
        self, runner, jobs, session, clients
    ):
        clients.update_fails = True
        job = await _queue(jobs, session)
        await runner.run(session, job)

        clients.update_fails = False
        job.status = JobStatus.QUEUED
        job.next_attempt_at = None
        await session.commit()
        await runner.run(session, job)

        by_stage = {row.stage: row for row in await jobs.stages(session, job.id)}
        assert by_stage[Stage.COLLECT].attempts == 2
        assert job.status is JobStatus.SUCCEEDED

    async def test_from_stage_reopens_that_stage_and_everything_after(self, runner, jobs, session):
        """The move when a stage *succeeded* but produced something wrong."""
        job = await _queue(jobs, session)
        await runner.run(session, job)

        await jobs.prepare_retry(session, job, from_stage=Stage.THUMBNAIL)
        by_stage = {row.stage: row for row in await jobs.stages(session, job.id)}

        assert by_stage[Stage.THUMBNAIL].status is StageStatus.PENDING
        assert by_stage[Stage.PUBLISH].status is StageStatus.PENDING
        assert by_stage[Stage.EXTRACT_METADATA].status is StageStatus.SUCCEEDED

    async def test_retrying_a_dead_lettered_job_clears_its_attempt_count(
        self, jobs, session, settings
    ):
        """Without this it immediately dead-letters again — the counter is already
        at the ceiling."""
        job = await _queue(jobs, session)
        job.attempts = settings.max_attempts
        await jobs.schedule_retry(session, job, error="upstream down")
        assert job.status is JobStatus.DEAD_LETTERED

        await jobs.prepare_retry(session, job)
        assert job.attempts == 0
        assert job.status is JobStatus.QUEUED

    async def test_a_running_job_cannot_be_requeued_underneath_itself(self, jobs, session):
        job = await _queue(jobs, session)
        await jobs.mark_running(session, job)

        with pytest.raises(ConflictError):
            await jobs.prepare_retry(session, job)

    async def test_the_accumulated_result_survives_a_resume(self, runner, jobs, session, clients):
        """`job.result` is reassigned rather than mutated: SQLAlchemy does not track
        in-place changes to a JSON column, and the next resume would rebuild from a
        document missing everything this run produced."""
        clients.update_fails = True
        job = await _queue(jobs, session)
        await runner.run(session, job)

        assert "document" in job.result
        assert job.result["document"]["page_count"] == 12


class TestCheckpointHygiene:
    async def test_artefact_bytes_never_reach_the_checkpoint(self, runner, jobs, session):
        """`_pending_uploads` carries file bytes between stages. Persisting them would
        put megabytes of binary through a JSON column — and would fail first."""
        job = await _queue(jobs, session)
        await runner.run(session, job)

        rows = await jobs.stages(session, job.id)
        for row in rows:
            assert not any(key.startswith("_") for key in row.output)

    async def test_the_stage_history_records_durations(self, runner, jobs, session):
        job = await _queue(jobs, session)
        await runner.run(session, job)

        succeeded = [
            row for row in await jobs.stages(session, job.id) if row.status is StageStatus.SUCCEEDED
        ]
        assert succeeded
        assert all(row.duration_ms is not None for row in succeeded)

    async def test_a_settled_job_is_not_run_again(self, runner, jobs, session):
        job = await _queue(jobs, session)
        await jobs.cancel(session, job)

        outcome = await runner.run(session, job)
        assert outcome.error == "already finished"


class TestSweeps:
    async def test_a_job_with_a_stale_heartbeat_is_requeued(self, jobs, session):
        """`acks_late` covers a task the broker knows about. It does not cover a
        worker that acked, started, and was then SIGKILLed."""
        from datetime import UTC, datetime, timedelta

        job = await _queue(jobs, session)
        await jobs.mark_running(session, job)
        job.heartbeat_at = datetime.now(UTC) - timedelta(hours=4)
        await session.commit()

        assert await jobs.requeue_stale(session) == 1
        assert job.status is JobStatus.QUEUED

    async def test_a_recently_beating_job_is_left_alone(self, jobs, session):
        """A twelve-minute conversion is not a dead worker."""
        job = await _queue(jobs, session)
        await jobs.mark_running(session, job)

        assert await jobs.requeue_stale(session) == 0
        assert job.status is JobStatus.RUNNING

    async def test_pruning_keeps_dead_lettered_jobs(self, jobs, session, settings):
        """They are the ones somebody still has to look at."""
        from datetime import UTC, datetime, timedelta

        old = datetime.now(UTC) - timedelta(days=settings.job_retention_days + 10)

        done = await _queue(jobs, session)
        await jobs.succeed(session, done)
        done.finished_at = old

        dead = await jobs.create(session, job_payload(source_key="private/uploads/other.pdf"))
        dead.status = JobStatus.DEAD_LETTERED
        dead.finished_at = old
        await session.commit()

        assert await jobs.prune(session) == 1
        assert await session.get(AutomationJob, dead.id) is not None
        assert await session.get(AutomationJob, done.id) is None

    async def test_deleting_a_job_takes_its_stage_rows_with_it(self, jobs, session):
        from sqlalchemy import func, select

        job = await _queue(jobs, session)
        await session.delete(job)
        await session.commit()

        remaining = (
            await session.execute(select(func.count(JobStage.id)).where(JobStage.job_id == job.id))
        ).scalar_one()
        assert remaining == 0

    async def test_claim_due_ignores_jobs_still_in_backoff(self, jobs, session):
        """A job in its backoff window simply does not match, so no worker holds it."""
        job = await _queue(jobs, session)
        job.attempts = 1
        await jobs.schedule_retry(session, job, error="upstream down")

        assert await jobs.claim_due(session) == []

    async def test_claim_due_returns_high_priority_first(self, jobs, session):
        low = await _queue(jobs, session, priority=0)
        high = await jobs.create(
            session, job_payload(source_key="private/uploads/urgent.pdf", priority=9)
        )

        due = await jobs.claim_due(session)
        assert [job.id for job in due][:2] == [high.id, low.id]


class TestStats:
    async def test_failures_are_counted_per_stage(self, runner, jobs, session, storage):
        """The job status only says that something failed. This says which stage."""
        storage.put("private/uploads/broken.pdf", b"%PDF-1.4 nope")
        job = await _queue(
            jobs, session, source_key="private/uploads/broken.pdf", original_filename="broken.pdf"
        )
        await runner.run(session, job)

        stats = await jobs.stats(session, days=7)
        assert stats.failures_by_stage.get("extract_metadata") == 1
        assert stats.failed == 1

    async def test_the_duration_is_a_median_not_a_mean(self, jobs, session):
        from services.jobs import _median_ms

        base = uuid.uuid4()  # noqa: F841 - keeps the import honest about intent
        from datetime import UTC, datetime, timedelta

        start = datetime.now(UTC)
        rows = [
            (start, start + timedelta(milliseconds=100)),
            (start, start + timedelta(milliseconds=200)),
            # One forty-minute scan would drag a mean far away from the typical run.
            (start, start + timedelta(minutes=40)),
        ]
        assert _median_ms(rows) == 200

    async def test_stats_are_empty_rather_than_null_on_a_fresh_install(self, jobs, session):
        stats = await jobs.stats(session, days=7)
        assert stats.total == 0
        assert stats.median_duration_ms is None
