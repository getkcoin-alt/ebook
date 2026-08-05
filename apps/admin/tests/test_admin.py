"""Aggregation isolation, token forwarding, and feature flag semantics."""

from __future__ import annotations

import uuid

import pytest

from schemas import PanelStatus
from tests.conftest import ADMIN_ID, TOKEN, USER_ID


class TestPanelIsolation:
    async def test_one_broken_service_does_not_break_the_page(self, aggregator, registry):
        """A dashboard with one status for the whole page goes red because one
        optional service is restarting, and then nobody sees the seven that are fine."""
        registry.behaviour["ai"] = "unreachable"
        board = await aggregator.dashboard(days=7)

        by_service = {panel.service: panel for panel in board.panels}
        assert by_service["ai"].status == PanelStatus.UNAVAILABLE
        assert by_service["payment"].status == PanelStatus.OK
        assert board.complete is False

    async def test_a_slow_service_times_out_alone(self, aggregator, registry, settings):
        registry.delay["search"] = settings.panel_timeout + 0.5
        board = await aggregator.dashboard(days=7)

        by_service = {panel.service: panel for panel in board.panels}
        assert by_service["search"].status == PanelStatus.TIMEOUT
        assert by_service["books"].status == PanelStatus.OK

    async def test_panels_are_fetched_concurrently(self, aggregator, registry, settings):
        """Serially the page costs the sum of every service's latency; concurrently
        it costs the slowest one. With seven panels at 0.3s, the difference is 2.1s
        against 0.3s — so the elapsed time is the assertion, not the outcome."""
        import time

        for name in settings.panel_services:
            registry.delay[name] = 0.3

        started = time.perf_counter()
        board = await aggregator.dashboard(days=7)
        elapsed = time.perf_counter() - started

        assert all(panel.status == PanelStatus.OK for panel in board.panels)
        assert elapsed < 1.0, f"panels look serial: {elapsed:.2f}s for 7 x 0.3s"

    async def test_an_upstream_error_message_is_not_passed_to_the_browser(
        self, aggregator, registry
    ):
        """An upstream error can carry internal hostnames and query fragments."""
        registry.behaviour["payment"] = "unreachable"
        board = await aggregator.dashboard(days=7)

        panel = next(p for p in board.panels if p.service == "payment")
        assert "db-prod-3.internal" not in (panel.error or "")

    async def test_a_403_is_reported_as_permission_not_as_an_outage(self, aggregator, registry):
        """So nobody goes looking for a failure that is really an authorisation
        decision."""
        registry.status["payment"] = 403
        board = await aggregator.dashboard(days=7)

        panel = next(p for p in board.panels if p.service == "payment")
        assert panel.status == PanelStatus.ERROR
        assert "permission" in (panel.error or "")

    async def test_every_panel_records_its_latency(self, aggregator, registry):
        board = await aggregator.dashboard(days=7)
        assert all(panel.latency_ms is not None for panel in board.panels)


class TestTokenForwarding:
    async def test_the_operators_token_reaches_each_service(self, aggregator, registry):
        """Without this the service is a confused deputy: reachable by anyone admin
        lets in, and holding an HMAC key that opens everything."""
        await aggregator.dashboard(days=7, token=TOKEN)

        forwarded = [call for call in registry.calls if call["headers"].get("Authorization")]
        assert forwarded
        assert all(call["headers"]["Authorization"] == f"Bearer {TOKEN}" for call in forwarded)

    async def test_internal_only_panels_do_not_receive_the_token(self, aggregator, registry):
        """The AI usage endpoint is HMAC-only; sending a bearer token there would be
        sending a credential somewhere that has no use for it."""
        await aggregator.dashboard(days=7, token=TOKEN)

        ai_call = next(call for call in registry.calls if call["service"] == "ai")
        assert not ai_call["headers"].get("Authorization")

    async def test_the_route_forwards_the_incoming_bearer_token(self, client, as_admin, registry):
        as_admin()
        await client.get("/v1/admin/dashboard")

        forwarded = [call for call in registry.calls if call["headers"].get("Authorization")]
        assert forwarded
        assert forwarded[0]["headers"]["Authorization"] == f"Bearer {TOKEN}"

    def test_a_non_bearer_header_yields_no_token(self):
        """A Basic credential must not be forwarded as if it were a bearer token."""
        from unittest.mock import Mock

        from deps import caller_token

        for header in ("", "Basic abc123", "Bearer ", "bearer"):
            request = Mock()
            request.headers = {"Authorization": header}
            assert caller_token(request) is None

    def test_a_lowercase_scheme_is_still_a_bearer_token(self):
        from unittest.mock import Mock

        from deps import caller_token

        request = Mock()
        request.headers = {"Authorization": "bearer abc"}
        assert caller_token(request) == "abc"


class TestCaching:
    async def test_a_second_render_does_not_fan_out_again(self, aggregator, registry):
        """A few operators watching a deploy would otherwise produce more internal
        traffic than the storefront."""
        await aggregator.dashboard(days=7)
        calls_after_first = len(registry.calls)

        board = await aggregator.dashboard(days=7)
        assert board.cached is True
        assert len(registry.calls) == calls_after_first

    async def test_refresh_bypasses_the_cache(self, aggregator, registry):
        await aggregator.dashboard(days=7)
        calls_after_first = len(registry.calls)

        board = await aggregator.dashboard(days=7, refresh=True)
        assert board.cached is False
        assert len(registry.calls) > calls_after_first

    async def test_a_different_window_is_a_different_cache_entry(self, aggregator, registry):
        await aggregator.dashboard(days=7)
        board = await aggregator.dashboard(days=30)

        assert board.cached is False
        assert board.window_days == 30

    async def test_the_health_board_is_cached_separately(self, aggregator, registry):
        await aggregator.dashboard(days=7)
        board = await aggregator.health()

        assert board.cached is False
        assert board.healthy == len(board.services)


class TestHealthBoard:
    async def test_it_reads_liveness_not_readiness(self, aggregator, registry):
        """Readiness goes red when a dependency is briefly slow — right for a load
        balancer, useless on a status board."""
        await aggregator.health()

        assert all(call["path"] == "/health" for call in registry.calls)

    async def test_a_down_service_is_counted_as_degraded(self, aggregator, registry):
        registry.behaviour["search"] = "unreachable"
        board = await aggregator.health()

        assert board.degraded == 1
        assert next(row for row in board.services if row.service == "search").status == "down"


class TestFlagEvaluation:
    async def test_an_unknown_flag_is_off_and_says_so(self, flags, session):
        """Defaulting an unknown flag to on would mean a typo silently enables an
        unfinished feature."""
        result = await flags.evaluate(session, "no.such.flag")

        assert result.enabled is False
        assert result.reason == "unknown_flag"

    async def test_a_fully_rolled_out_flag_is_on_for_everyone(self, flags, session):
        from schemas import FlagCreate

        await flags.create(session, FlagCreate(key="new.checkout", enabled=True), actor_id=ADMIN_ID)

        result = await flags.evaluate(session, "new.checkout", user_id=USER_ID)
        assert result.enabled is True
        assert result.reason == "fully_rolled_out"

    async def test_a_disabled_flag_is_off_regardless_of_rollout(self, flags, session):
        from schemas import FlagCreate

        await flags.create(
            session,
            FlagCreate(key="beta", enabled=False, rollout_percent=100),
            actor_id=ADMIN_ID,
        )

        assert (await flags.evaluate(session, "beta", user_id=USER_ID)).enabled is False

    async def test_the_allowlist_beats_the_rollout(self, flags, session):
        """How a flag is tested in production without being turned on for everybody."""
        from schemas import FlagCreate

        await flags.create(
            session,
            FlagCreate(key="beta", enabled=True, rollout_percent=0, allowlist=[USER_ID]),
            actor_id=ADMIN_ID,
        )

        result = await flags.evaluate(session, "beta", user_id=USER_ID)
        assert result.enabled is True
        assert result.reason == "allowlisted"

    async def test_a_partial_rollout_is_off_for_an_anonymous_caller(self, flags, session):
        """A random bucket would make the same page flicker between variants on
        reload."""
        from schemas import FlagCreate

        await flags.create(
            session,
            FlagCreate(key="beta", enabled=True, rollout_percent=50),
            actor_id=ADMIN_ID,
        )

        result = await flags.evaluate(session, "beta", user_id=None)
        assert result.enabled is False
        assert result.reason == "no_user_for_rollout"

    async def test_bucketing_is_stable_across_calls(self, flags, session):
        """The same user gets the same answer every time, across restarts and across
        services."""
        from schemas import FlagCreate

        await flags.create(
            session,
            FlagCreate(key="beta", enabled=True, rollout_percent=50),
            actor_id=ADMIN_ID,
        )

        answers = {
            (await flags.evaluate(session, "beta", user_id=USER_ID)).enabled for _ in range(5)
        }
        assert len(answers) == 1

    async def test_raising_a_rollout_never_removes_anyone(self, flags, session):
        """A rollout that reshuffles reads to a user as a bug in the feature."""
        from services import bucket_of

        users = [uuid.uuid4() for _ in range(200)]
        at_20 = {user for user in users if bucket_of("beta", user) < 20}
        at_60 = {user for user in users if bucket_of("beta", user) < 60}

        assert at_20 <= at_60

    async def test_buckets_differ_per_flag(self, flags, session):
        """Hashed with the flag key, not the user alone — otherwise the same unlucky
        5% would be the first cohort for every flag on the platform."""
        from services import bucket_of

        users = [uuid.uuid4() for _ in range(300)]
        first = {user for user in users if bucket_of("flag.one", user) < 10}
        second = {user for user in users if bucket_of("flag.two", user) < 10}

        assert first != second

    async def test_buckets_are_spread_across_the_range(self, flags, session):
        from services import bucket_of

        buckets = [bucket_of("beta", uuid.uuid4()) for _ in range(500)]
        # A rollout at 50% should catch roughly half. Wide bounds — this is checking
        # the hash is not degenerate, not that it is uniform.
        caught = sum(1 for bucket in buckets if bucket < 50)
        assert 175 < caught < 325


class TestFlagAdministration:
    async def test_creating_a_flag_writes_an_audit_row(self, flags, session):
        from schemas import FlagCreate

        await flags.create(session, FlagCreate(key="beta"), actor_id=ADMIN_ID)
        rows, total = await flags.audits(session)

        assert total == 1
        assert rows[0].action == "created"

    async def test_an_update_records_both_sides(self, flags, session):
        """ "Enabled payments" is a far less useful record than "rollout went from 5%
        to 100%"."""
        from schemas import FlagCreate, FlagUpdate

        await flags.create(
            session,
            FlagCreate(key="beta", enabled=True, rollout_percent=5),
            actor_id=ADMIN_ID,
        )
        await flags.update(
            session,
            "beta",
            FlagUpdate(rollout_percent=100, reason="ship it"),
            actor_id=ADMIN_ID,
        )

        rows, _total = await flags.audits(session, key="beta")
        latest = rows[0]
        assert latest.before["rollout_percent"] == 5
        assert latest.after["rollout_percent"] == 100
        assert latest.reason == "ship it"

    async def test_a_duplicate_key_is_a_conflict_not_a_crash(self, flags, session):
        from knowledgeos_core import ConflictError
        from schemas import FlagCreate

        await flags.create(session, FlagCreate(key="beta"), actor_id=ADMIN_ID)

        with pytest.raises(ConflictError):
            await flags.create(session, FlagCreate(key="beta"), actor_id=ADMIN_ID)

    async def test_the_transaction_survives_a_duplicate(self, flags, session):
        """The SAVEPOINT opens before `session.add` — adding first would emit the
        INSERT outside it and take the surrounding transaction down."""
        from knowledgeos_core import ConflictError
        from schemas import FlagCreate

        await flags.create(session, FlagCreate(key="beta"), actor_id=ADMIN_ID)
        with pytest.raises(ConflictError):
            await flags.create(session, FlagCreate(key="beta"), actor_id=ADMIN_ID)

        # The session is still usable, which is the actual assertion.
        await flags.create(session, FlagCreate(key="gamma"), actor_id=ADMIN_ID)
        assert len(await flags.list_flags(session)) == 2

    async def test_deleting_a_flag_keeps_its_history(self, flags, session):
        """Otherwise the record of a flag that was on during an incident is lost."""
        from schemas import FlagCreate

        await flags.create(session, FlagCreate(key="beta"), actor_id=ADMIN_ID)
        await flags.delete(session, "beta", actor_id=ADMIN_ID, reason="cleaned up")

        _rows, total = await flags.audits(session, key="beta")
        assert total == 2

    @pytest.mark.parametrize(
        "key", ["Beta Flag", "beta:flag", "1beta", "b", "beta flag", "beta/flag"]
    )
    def test_invalid_keys_are_rejected(self, key):
        """The key is embedded in cache keys and read by every service."""
        from pydantic import ValidationError

        from schemas import FlagCreate

        with pytest.raises(ValidationError):
            FlagCreate(key=key)

    @pytest.mark.parametrize("key", ["beta", "new.checkout", "ai_summaries", "a-b.c_d"])
    def test_valid_keys_are_accepted(self, key):
        from schemas import FlagCreate

        assert FlagCreate(key=key).key == key

    def test_keys_are_normalised_to_lower_case(self):
        from schemas import FlagCreate

        assert FlagCreate(key="  Beta.Flag  ").key == "beta.flag"


class TestApi:
    async def test_the_dashboard_needs_a_permission(self, client, as_admin):
        as_admin(permissions=["books:read"])
        assert (await client.get("/v1/admin/dashboard")).status_code == 403

    async def test_the_dashboard_renders(self, client, as_admin, settings):
        as_admin()
        response = await client.get("/v1/admin/dashboard", params={"days": 30})

        assert response.status_code == 200
        assert response.json()["window_days"] == 30
        assert len(response.json()["panels"]) == len(settings.panel_services)

    async def test_the_health_board_renders(self, client, as_admin):
        as_admin()
        response = await client.get("/v1/admin/health-board")

        assert response.status_code == 200
        assert response.json()["degraded"] == 0

    async def test_flags_are_creatable_and_listable(self, client, as_admin):
        as_admin()
        created = await client.post(
            "/v1/admin/flags", json={"key": "new.checkout", "enabled": True}
        )
        assert created.status_code == 201

        listing = await client.get("/v1/admin/flags")
        assert [flag["key"] for flag in listing.json()] == ["new.checkout"]

    async def test_creating_a_flag_needs_write_not_read(self, client, as_admin):
        as_admin(permissions=["analytics:read"])
        response = await client.post("/v1/admin/flags", json={"key": "beta"})
        assert response.status_code == 403

    async def test_a_flag_can_be_updated_and_its_audit_read(self, client, as_admin):
        as_admin()
        await client.post("/v1/admin/flags", json={"key": "beta", "rollout_percent": 10})
        await client.patch(
            "/v1/admin/flags/beta",
            json={"rollout_percent": 100, "reason": "ship"},
        )

        audit = await client.get("/v1/admin/flags/beta/audit")
        assert audit.json()["total"] == 2
        assert audit.json()["items"][0]["after"]["rollout_percent"] == 100

    async def test_an_unknown_flag_is_a_404(self, client, as_admin):
        as_admin()
        assert (await client.get("/v1/admin/flags/nope")).status_code == 404


class TestInternalRoutes:
    async def test_flag_evaluation_is_signed(self, client):
        """Private-network reachability is not authorisation."""
        response = await client.post("/internal/flags/evaluate", json={"keys": ["beta"]})
        assert response.status_code in (401, 403)

    async def test_a_service_gets_decisions_not_rules(self, client, as_admin, as_internal):
        """Handing back a rollout percentage would make every service implement the
        bucketing itself, and two implementations diverge."""
        as_admin()
        await client.post(
            "/v1/admin/flags",
            json={"key": "beta", "enabled": True, "rollout_percent": 50},
        )

        as_internal("books")
        response = await client.post(
            "/internal/flags/evaluate",
            json={"keys": ["beta"], "user_id": str(USER_ID)},
        )

        body = response.json()
        assert isinstance(body["flags"]["beta"], bool)
        assert "rollout_percent" not in body

    async def test_unknown_keys_are_reported_and_answered(self, client, as_admin, as_internal):
        """Reporting makes a typo visible; answering keeps a caller that only reads
        `flags` from raising."""
        as_internal("books")
        response = await client.post("/internal/flags/evaluate", json={"keys": ["typo.flag"]})

        body = response.json()
        assert body["unknown"] == ["typo.flag"]
        assert body["flags"]["typo.flag"] is False

    async def test_one_flag_can_be_evaluated_with_a_reason(self, client, as_admin, as_internal):
        as_admin()
        await client.post("/v1/admin/flags", json={"key": "beta", "enabled": True})

        as_internal("books")
        response = await client.get("/internal/flags/beta", params={"user_id": str(USER_ID)})

        assert response.json()["enabled"] is True
        assert response.json()["reason"] == "fully_rolled_out"

    async def test_the_audit_prune_reports_what_it_dropped(self, client, as_internal):
        as_internal("workers")
        response = await client.post("/internal/maintenance/prune-audits")
        assert "0" in response.json()["message"]


class TestPayloadFlattening:
    def test_lists_become_counts(self):
        """A card does not render a thousand rows, and `total` is what it was going to
        show anyway."""
        import httpx

        from services.aggregator import _flatten

        response = httpx.Response(200, json={"items": [1, 2, 3], "total": 3})
        assert _flatten(response) == {"items_count": 3, "total": 3}

    def test_long_strings_are_dropped(self):
        import httpx

        from services.aggregator import _flatten

        response = httpx.Response(200, json={"blurb": "x" * 500, "count": 2})
        assert _flatten(response) == {"count": 2}

    def test_a_non_object_body_is_handled(self):
        import httpx

        from services.aggregator import _flatten

        assert _flatten(httpx.Response(200, json=[1, 2])) == {"count": 2}
        assert _flatten(httpx.Response(200, text="not json")) == {}
