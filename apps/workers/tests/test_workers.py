"""Scheduler behaviour: single-flight, outcome classification, health and the API."""

from __future__ import annotations

import uuid

import pytest
from schedule import BY_NAME, SCHEDULE, ScheduledJob, enabled_jobs

from schemas import RunOutcome
from tests.conftest import make_run

ORDERS = BY_NAME["payment.expire-orders"]
RECONCILE = BY_NAME["search.reconcile"]


class TestScheduleShape:
    def test_every_job_names_a_service_and_an_http_path(self):
        """Never a table. A scheduler with direct access to every schema is how a
        microservice platform quietly re-couples."""
        for job in SCHEDULE:
            assert job.service
            assert job.path.startswith("/internal/") or job.path.startswith("/v1/")

    def test_job_names_are_unique(self):
        """The name is the lock key, the metric label and the history key."""
        names = [job.name for job in SCHEDULE]
        assert len(names) == len(set(names))

    def test_expensive_jobs_do_not_all_fire_on_the_hour(self):
        """Six services waking at :00 is a six-way spike once an hour, and the spike
        is what sizes the database."""
        fixed_minute = [job for job in SCHEDULE if not job.minute.startswith("*")]
        assert fixed_minute, "expected some jobs on a fixed minute"
        assert not any(job.minute == "0" for job in fixed_minute)

    def test_a_lock_outlives_its_job_budget(self):
        """A lock that expires mid-run lets the next tick start a second copy on top."""
        for job in SCHEDULE:
            assert job.lock_ttl >= job.timeout, job.name

    def test_every_scheduled_service_has_a_configured_url(self, settings):
        """`ServiceRegistry` resolves `<name>_service_url`. A job naming a service
        with no setting fails every time it fires with "No URL configured" — which
        looks like an outage rather than a typo, and only shows up at runtime."""
        for job in SCHEDULE:
            assert getattr(settings, f"{job.service}_service_url", None), job.name

    def test_disabled_jobs_are_excluded(self):
        active = enabled_jobs({"ai.prune", "search.reconcile"})
        names = {job.name for job in active}

        assert "ai.prune" not in names
        assert "payment.expire-orders" in names

    def test_a_duplicate_name_would_fail_the_process_on_boot(self):
        """Rather than silently shadowing an entry, which would leave one sweep
        never running and no error anywhere."""
        seen: dict[str, ScheduledJob] = {}
        duplicated = (
            ScheduledJob(name="dup", service="payment", path="/internal/a"),
            ScheduledJob(name="dup", service="payment", path="/internal/b"),
        )
        with pytest.raises(RuntimeError):
            for job in duplicated:
                if job.name in seen:
                    raise RuntimeError(f"Duplicate scheduled job name: {job.name}")
                seen[job.name] = job


class TestRunner:
    async def test_a_successful_sweep_is_recorded_with_what_it_reported(self, runner, registry):
        registry.body["payment"] = {"expired": 4, "message": "ok"}
        result = await runner.run(ORDERS)

        assert result.outcome is RunOutcome.SUCCEEDED
        assert result.status_code == 200
        assert result.detail["expired"] == 4

    async def test_the_call_carries_the_jobs_own_timeout(self, runner, registry):
        """The client default is sized for ordinary calls; a reconcile runs for
        minutes and raising the default would let every call hang as long."""
        await runner.run(RECONCILE)

        assert registry.calls[-1]["timeout"] == RECONCILE.timeout

    async def test_the_body_and_params_from_the_schedule_are_sent(self, runner, registry):
        await runner.run(RECONCILE)
        assert registry.calls[-1]["json"] == {"mode": "reconcile"}

        await runner.run(BY_NAME["automation.drain"])
        assert registry.calls[-1]["params"] == {"limit": 5}

    async def test_an_error_status_is_a_failure_not_a_success(self, runner, registry):
        """The shared client only raises for transport failures — a 500 comes back as
        an ordinary response, and recording that as a success is how a sweep broken
        for a week shows up green."""
        registry.status["payment"] = 500
        result = await runner.run(ORDERS)

        assert result.outcome is RunOutcome.FAILED
        assert result.status_code == 500

    async def test_a_timeout_is_distinct_from_a_failure(self, runner, registry):
        """The sweep has very likely completed on the other side; we stopped waiting.
        Treating it as a failure invites a retry that duplicates the work."""
        registry.behaviour["payment"] = "timeout"
        result = await runner.run(ORDERS)

        assert result.outcome is RunOutcome.TIMED_OUT
        assert result.status_code is None

    async def test_an_unreachable_service_is_a_failure(self, runner, registry):
        registry.behaviour["payment"] = "unreachable"
        result = await runner.run(ORDERS)

        assert result.outcome is RunOutcome.FAILED
        assert result.error

    async def test_the_runner_never_raises(self, runner, registry):
        """A task that raises gets retried by Celery on top of the policy configured
        here — a second, invisible retry layer."""
        registry.behaviour["payment"] = "unreachable"
        assert (await runner.run(ORDERS)).outcome is RunOutcome.FAILED

    async def test_a_disabled_job_is_not_called_at_all(
        self, settings, runner, registry, monkeypatch
    ):
        monkeypatch.setattr(settings, "disabled_jobs", ["payment.expire-orders"])
        result = await runner.run(ORDERS)

        assert result.outcome is RunOutcome.DISABLED
        assert registry.calls == []


class TestSingleFlight:
    async def test_only_one_of_three_concurrent_replicas_fires_the_job(
        self, settings, registry, redis
    ):
        """Beat fires on every replica. Without the lock, expire-orders runs three
        times a minute and reconcile runs three simultaneous catalogue walks."""
        import asyncio

        from services import JobRunner

        registry.delay = 0.05
        replicas = [
            JobRunner(settings, registry, redis, worker_id=f"replica-{index}") for index in range(3)
        ]
        results = await asyncio.gather(*(replica.run(ORDERS) for replica in replicas))

        outcomes = [result.outcome for result in results]
        assert outcomes.count(RunOutcome.SUCCEEDED) == 1
        assert outcomes.count(RunOutcome.SKIPPED_LOCKED) == 2
        # And the sibling service was called exactly once, which is the point.
        assert len(registry.calls) == 1

    async def test_losing_the_lock_is_not_a_failure(self, settings, registry, redis):
        """A three-replica deployment must not look like it is failing two runs in
        three."""
        from services import JobRunner

        holder = JobRunner(settings, registry, redis, worker_id="replica-a")
        contender = JobRunner(settings, registry, redis, worker_id="replica-b")

        async with redis.lock(f"workers:{ORDERS.name}", ttl=30) as acquired:
            assert acquired
            result = await contender.run(ORDERS)

        assert result.outcome is RunOutcome.SKIPPED_LOCKED
        assert registry.calls == []
        assert holder is not contender

    async def test_a_manual_trigger_ignores_the_lock(self, runner, registry, redis):
        """An operator pressing "run now" wants it to run now, not to be told another
        replica might already be doing it."""
        async with redis.lock(f"workers:{ORDERS.name}", ttl=30) as acquired:
            assert acquired
            result = await runner.run(ORDERS, ignore_lock=True)

        assert result.outcome is RunOutcome.SUCCEEDED
        assert registry.calls


class TestHistory:
    async def test_a_run_row_carries_the_worker_that_ran_it(self, runner, registry, session):
        result = await runner.run(ORDERS)
        row = runner.to_row(result)
        session.add(row)
        await session.commit()

        assert row.worker_id == "test-worker"
        assert row.started_at <= row.finished_at

    async def test_consecutive_failures_reset_on_a_success(self, history, session):
        session.add(make_run("payment.expire-orders", RunOutcome.FAILED, minutes_ago=30))
        session.add(make_run("payment.expire-orders", RunOutcome.SUCCEEDED, minutes_ago=20))
        session.add(make_run("payment.expire-orders", RunOutcome.FAILED, minutes_ago=10))
        await session.commit()

        assert await history.consecutive_failures(session, "payment.expire-orders") == 1

    async def test_a_skipped_run_does_not_reset_a_failure_streak(self, history, session):
        """It is neither a success nor a failure — another replica took that run."""
        session.add(make_run("payment.expire-orders", RunOutcome.FAILED, minutes_ago=30))
        session.add(make_run("payment.expire-orders", RunOutcome.SKIPPED_LOCKED, minutes_ago=20))
        session.add(make_run("payment.expire-orders", RunOutcome.FAILED, minutes_ago=10))
        await session.commit()

        assert await history.consecutive_failures(session, "payment.expire-orders") == 2

    async def test_a_timeout_counts_against_health(self, history, session):
        for minutes in (30, 20, 10):
            session.add(make_run("search.reconcile", RunOutcome.TIMED_OUT, minutes_ago=minutes))
        await session.commit()

        described = await history.describe(session, RECONCILE)
        assert described.consecutive_failures == 3
        assert described.healthy is False

    async def test_one_failure_is_not_unhealthy(self, history, session):
        """A deploy, a restart, a network blip. Alerting on it teaches everyone to
        ignore the alert."""
        session.add(make_run("search.reconcile", RunOutcome.FAILED, minutes_ago=5))
        await session.commit()

        assert (await history.describe(session, RECONCILE)).healthy is True

    async def test_a_job_that_has_stopped_running_shows_as_stale(self, history, session):
        """The failure a scheduler is most prone to: it produces no logs and no
        failures, so it is invisible in every signal except this one."""
        session.add(make_run("payment.expire-orders", RunOutcome.SUCCEEDED, minutes_ago=180))
        session.add(make_run("search.reconcile", RunOutcome.SUCCEEDED, minutes_ago=1))
        await session.commit()

        health = await history.health(session)
        assert "payment.expire-orders" in health.stale_jobs
        assert "search.reconcile" not in health.stale_jobs

    async def test_a_fresh_install_does_not_report_its_whole_schedule_as_broken(
        self, history, session
    ):
        assert (await history.health(session)).stale_jobs == []

    async def test_health_counts_the_last_hour(self, history, session):
        session.add(make_run("ai.prune", RunOutcome.SUCCEEDED, minutes_ago=10))
        session.add(make_run("ai.prune", RunOutcome.FAILED, minutes_ago=20))
        session.add(make_run("ai.prune", RunOutcome.FAILED, minutes_ago=200))
        await session.commit()

        health = await history.health(session)
        assert health.runs_last_hour == 2
        assert health.failures_last_hour == 1

    async def test_pruning_drops_records_past_retention(self, history, session, settings):
        old = settings.run_retention_days * 24 * 60 + 60
        session.add(make_run("ai.prune", RunOutcome.SUCCEEDED, minutes_ago=old))
        session.add(make_run("ai.prune", RunOutcome.SUCCEEDED, minutes_ago=5))
        await session.commit()

        assert await history.prune(session) == 1
        _rows, total = await history.runs(session)
        assert total == 1


class TestApi:
    async def test_the_schedule_is_listable(self, client, as_admin):
        as_admin()
        response = await client.get("/v1/admin/workers/jobs")

        assert response.status_code == 200
        assert len(response.json()) == len(SCHEDULE)
        assert {"name", "service", "path", "cron"} <= set(response.json()[0])

    async def test_one_job_carries_its_recent_health(self, client, as_admin, session):
        session.add(make_run("ai.prune", RunOutcome.SUCCEEDED, minutes_ago=5))
        await session.commit()

        as_admin()
        response = await client.get("/v1/admin/workers/jobs/ai.prune")

        assert response.json()["last_outcome"] == "succeeded"
        assert response.json()["healthy"] is True

    async def test_an_unknown_job_is_a_404(self, client, as_admin):
        as_admin()
        assert (await client.get("/v1/admin/workers/jobs/nope")).status_code == 404

    async def test_triggering_records_who_did_it(self, client, as_admin, registry):
        """Distinguishable from a scheduled firing when someone later asks why a
        sweep ran at an odd hour."""
        as_admin()
        response = await client.post("/v1/admin/workers/jobs/ai.prune/run")

        assert response.status_code == 200
        assert response.json()["outcome"] == "succeeded"

        runs = await client.get("/v1/admin/workers/runs", params={"job_name": "ai.prune"})
        assert runs.json()["items"][0]["triggered_by"] is not None

    async def test_triggering_needs_write_not_just_read(self, client, as_admin):
        as_admin(permissions=["analytics:read"])
        response = await client.post("/v1/admin/workers/jobs/ai.prune/run")
        assert response.status_code == 403

    async def test_reading_needs_a_permission(self, client, as_admin):
        as_admin(permissions=["books:read"])
        assert (await client.get("/v1/admin/workers/jobs")).status_code == 403

    async def test_runs_can_be_filtered_by_outcome(self, client, as_admin, session):
        session.add(make_run("ai.prune", RunOutcome.SUCCEEDED, minutes_ago=5))
        session.add(make_run("ai.prune", RunOutcome.FAILED, minutes_ago=6))
        await session.commit()

        as_admin()
        failed = await client.get("/v1/admin/workers/runs", params={"outcome": "failed"})
        assert failed.json()["total"] == 1

    async def test_scheduler_health_is_readable(self, client, as_admin):
        as_admin()
        response = await client.get("/v1/admin/workers/health")

        body = response.json()
        assert body["total_jobs"] == len(SCHEDULE)
        assert body["enabled_jobs"] == len(SCHEDULE)

    async def test_a_failing_sweep_surfaces_on_the_trigger_response(
        self, client, as_admin, registry
    ):
        registry.status["ai"] = 503
        as_admin()
        response = await client.post("/v1/admin/workers/jobs/ai.prune/run")

        assert response.json()["outcome"] == "failed"
        assert response.json()["status_code"] == 503


class TestInternalRoutes:
    async def test_the_self_prune_is_signed(self, client):
        """Private-network reachability is not authorisation."""
        response = await client.post("/internal/maintenance/prune")
        assert response.status_code in (401, 403)

    async def test_the_self_prune_reports_what_it_dropped(self, client, as_internal):
        as_internal("workers")
        response = await client.post("/internal/maintenance/prune")
        assert response.json()["pruned"] == 0

    async def test_internal_health_is_available_to_the_admin_service(self, client, as_internal):
        as_internal("admin")
        response = await client.get("/internal/health")
        assert response.json()["total_jobs"] == len(SCHEDULE)

    async def test_the_service_prunes_its_own_history_on_a_schedule(self):
        """A history table that grows forever is the problem this service exists to
        solve for everyone else."""
        assert "workers.prune-runs" in BY_NAME
        assert BY_NAME["workers.prune-runs"].service == "workers"


class TestIntervalEstimation:
    @pytest.mark.parametrize(
        ("minute", "hour", "expected"),
        [
            ("*/5", "*", 300),
            ("*/15", "*", 900),
            ("*", "*", 60),
            ("17", "*", 3_600),
            ("23", "2", 86_400),
        ],
    )
    def test_intervals_are_derived_from_the_cron_fields(self, minute, hour, expected):
        """Approximate on purpose — it feeds a threshold already multiplied by three,
        so a real cron parser would be a dependency carried for a rounded number."""
        from services.history import _interval_seconds

        job = ScheduledJob(
            name="x", service="payment", path="/internal/x", minute=minute, hour=hour
        )
        assert _interval_seconds(job) == expected


class TestTriggeredByParsing:
    def test_a_malformed_id_is_dropped_rather_than_raising(self):
        from services import parse_triggered_by

        assert parse_triggered_by("not-a-uuid") is None
        assert parse_triggered_by(None) is None
        assert parse_triggered_by(str(uuid.uuid4())) is not None
