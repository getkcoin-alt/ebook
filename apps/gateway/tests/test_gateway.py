"""Gateway behaviour: routing, header hygiene, cache isolation, limits, failures."""

from __future__ import annotations

import httpx
import pytest

pytestmark = pytest.mark.asyncio


class TestRouting:
    async def test_request_reaches_the_right_upstream(self, client, upstream_log):
        response = await client.get("/v1/books")
        assert response.status_code == 200
        assert response.json()["upstream"] == "books.test"

    async def test_longest_prefix_wins(self, client, as_user, upstream_log):
        as_user()
        await client.get("/v1/books/downloads/abc", headers={"Authorization": "Bearer x"})
        # /v1/books/downloads must beat /v1/books, or downloads would be cached.
        assert upstream_log[-1]["path"] == "/v1/books/downloads/abc"

    async def test_unmatched_path_is_404(self, client):
        response = await client.get("/v1/nonexistent")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "resource_not_found"

    async def test_query_string_is_forwarded(self, client, upstream_log):
        await client.get("/v1/books?category=fiction&limit=5")
        assert "category=fiction" in upstream_log[-1]["query"]

    async def test_path_is_preserved_exactly(self, client, upstream_log):
        await client.get("/v1/books/some-slug-here")
        assert upstream_log[-1]["path"] == "/v1/books/some-slug-here"

    async def test_the_shared_admin_prefix_splits_by_owning_service(self):
        """`/v1/admin` is shared: the admin service owns it in general, but each
        service serves the admin surface for the data it owns. Without the explicit
        entries these would all be proxied to `admin`, which has never heard of a
        coupon."""
        from routes import resolve

        assert resolve("/v1/admin/settings").upstream == "admin"
        assert resolve("/v1/admin/books/abc").upstream == "books"
        assert resolve("/v1/admin/orders/abc/refund").upstream == "payment"
        assert resolve("/v1/admin/coupons").upstream == "payment"
        assert resolve("/v1/admin/revenue").upstream == "payment"

    async def test_payment_read_paths_are_reachable_without_a_token(self):
        """The checkout page prices a cart before the customer signs in."""
        from routes import resolve

        for path in ("/v1/checkout/quote", "/v1/coupons/validate", "/v1/payments/providers"):
            route = resolve(path)
            assert route.upstream == "payment", path
            assert route.public is True, path

    async def test_webhook_paths_are_never_cached(self):
        """The payment service verifies the signature over the raw body, so nothing
        between the provider and it may alter or replay those bytes."""
        from routes import resolve

        route = resolve("/v1/webhooks/razorpay")
        assert route.upstream == "payment"
        assert route.cache_ttl == 0


class TestHeaderHygiene:
    async def test_hop_by_hop_headers_are_stripped(self, client, upstream_log):
        # Forwarding these produces framing bugs that look like random truncation.
        await client.get(
            "/v1/books",
            headers={
                # A distinctive value, so we can tell the client's header apart from
                # the one httpx legitimately sets on its own outbound connection.
                "Connection": "close",
                "Keep-Alive": "timeout=5",
                "Transfer-Encoding": "chunked",
                "Upgrade": "websocket",
                "TE": "trailers",
                "Proxy-Authorization": "Basic abc",
            },
        )
        forwarded = {k.lower(): v for k, v in upstream_log[-1]["headers"].items()}
        for banned in (
            "keep-alive",
            "transfer-encoding",
            "upgrade",
            "te",
            "proxy-authorization",
        ):
            assert banned not in forwarded, f"{banned} was forwarded upstream"
        # httpx manages the upstream connection and sets its own Connection header;
        # what must not survive is the *client's* value.
        assert forwarded.get("connection") != "close"

    async def test_client_cannot_spoof_the_identity_headers(self, client, upstream_log):
        # Trivial privilege escalation if the gateway passes these through.
        await client.get(
            "/v1/books",
            headers={
                "X-Authenticated-User": "admin-666",
                "X-Authenticated-Roles": "superadmin",
            },
        )
        headers = {k.lower(): v for k, v in upstream_log[-1]["headers"].items()}
        assert headers.get("x-authenticated-user") != "admin-666"
        assert headers.get("x-authenticated-roles") != "superadmin"

    async def test_authenticated_identity_is_asserted_upstream(self, client, as_user, upstream_log):
        as_user(user_id="reader-9", roles=["author"])
        await client.get("/v1/auth/me", headers={"Authorization": "Bearer x"})
        headers = {k.lower(): v for k, v in upstream_log[-1]["headers"].items()}
        assert headers["x-authenticated-user"] == "reader-9"
        assert headers["x-authenticated-roles"] == "author"

    async def test_internal_signature_is_attached(self, client, upstream_log):
        await client.get("/v1/books")
        headers = {k.lower(): v for k, v in upstream_log[-1]["headers"].items()}
        assert headers["x-internal-service"] == "gateway"
        assert headers["x-internal-signature"]
        assert headers["x-internal-timestamp"]

    async def test_request_id_is_propagated_and_returned(self, client, upstream_log):
        response = await client.get("/v1/books", headers={"X-Request-ID": "trace-me"})
        headers = {k.lower(): v for k, v in upstream_log[-1]["headers"].items()}
        assert headers["x-request-id"] == "trace-me"
        assert response.headers["X-Request-ID"] == "trace-me"

    async def test_forwarded_for_chain_is_appended_not_replaced(self, client, upstream_log):
        await client.get("/v1/books", headers={"X-Forwarded-For": "203.0.113.9"})
        headers = {k.lower(): v for k, v in upstream_log[-1]["headers"].items()}
        assert headers["x-forwarded-for"].startswith("203.0.113.9")


class TestResponseCache:
    async def test_public_get_is_cached(self, client, upstream_log):
        first = await client.get("/v1/books")
        assert first.headers["X-Cache"] == "MISS"
        calls_after_first = len(upstream_log)

        second = await client.get("/v1/books")
        assert second.headers["X-Cache"] == "HIT"
        # A hit must not touch the upstream at all.
        assert len(upstream_log) == calls_after_first

    async def test_authenticated_response_is_never_served_to_anonymous(
        self, client, as_user, upstream_log
    ):
        """The cross-user leak this cache is designed to prevent.

        /v1/library varies per user. An entry stored for one caller must never be
        returned to another, and never to an anonymous caller.
        """
        as_user(user_id="alice")
        alice = await client.get("/v1/library", headers={"Authorization": "Bearer alice"})
        assert alice.status_code == 200

        # Anonymous request to the same path must not hit Alice's entry. The route
        # requires auth, so the correct outcome is 401 — never Alice's body.
        anonymous = await client.get("/v1/library")
        assert anonymous.status_code == 401
        assert anonymous.headers.get("X-Cache") != "HIT"

    async def test_two_users_do_not_share_a_cache_entry(self, client, as_user, upstream_log):
        as_user(user_id="alice")
        await client.get("/v1/library", headers={"Authorization": "Bearer alice"})
        before = len(upstream_log)

        as_user(user_id="bob")
        bob = await client.get("/v1/library", headers={"Authorization": "Bearer bob"})
        # Bob must reach the upstream rather than being served Alice's cached page.
        assert len(upstream_log) > before
        assert bob.headers["X-Cache"] == "MISS"

    async def test_authenticated_caller_bypasses_a_non_user_varying_cache(
        self, client, as_user, upstream_log
    ):
        # /v1/books does not vary on user, so an authenticated response is not
        # cached at all rather than risk storing something personalised.
        as_user()
        response = await client.get("/v1/books", headers={"Authorization": "Bearer x"})
        assert response.headers["X-Cache"] == "BYPASS"

    async def test_query_string_is_part_of_the_key(self, client, upstream_log):
        await client.get("/v1/books?page=1")
        before = len(upstream_log)
        await client.get("/v1/books?page=2")
        assert len(upstream_log) > before  # different query, different entry

    async def test_no_cache_request_header_is_honoured(self, client, upstream_log):
        await client.get("/v1/books")
        before = len(upstream_log)
        response = await client.get("/v1/books", headers={"Cache-Control": "no-cache"})
        assert len(upstream_log) > before
        assert response.headers["X-Cache"] != "HIT"

    async def test_post_is_never_cached(self, client, upstream_log):
        await client.post("/v1/books", json={"title": "x"})
        before = len(upstream_log)
        await client.post("/v1/books", json={"title": "x"})
        assert len(upstream_log) > before

    async def test_uncacheable_route_reports_bypass(self, client, as_user):
        as_user()
        response = await client.get("/v1/wishlist", headers={"Authorization": "Bearer x"})
        assert response.headers["X-Cache"] == "BYPASS"


class TestAuthentication:
    async def test_protected_route_requires_a_token(self, client):
        response = await client.get("/v1/auth/me")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"

    async def test_public_route_works_anonymously(self, client):
        assert (await client.get("/v1/books")).status_code == 200

    async def test_login_is_public(self, client):
        assert (await client.post("/v1/auth/login", json={})).status_code == 200

    async def test_webhooks_are_public(self, client):
        # Providers send no user token; the payload signature authenticates them.
        assert (await client.post("/v1/webhooks/stripe", json={})).status_code == 200
        assert (await client.post("/v1/webhooks/razorpay", json={})).status_code == 200

    async def test_banned_user_is_blocked(self, client, as_user, app):
        principal = as_user(user_id="banned-user")
        await app.state.ctx.redis.client.set(f"kos:denylist:user:{principal.user_id}", b"spam")
        response = await client.get("/v1/auth/me", headers={"Authorization": "Bearer x"})
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "account_banned"

    async def test_unbanned_user_passes(self, client, as_user):
        as_user(user_id="ok-user")
        response = await client.get("/v1/auth/me", headers={"Authorization": "Bearer x"})
        assert response.status_code == 200


class TestRateLimiting:
    async def test_rate_limit_headers_are_returned(self, client):
        response = await client.get("/v1/books")
        assert "X-RateLimit-Limit" in response.headers
        assert "X-RateLimit-Remaining" in response.headers

    async def test_limit_is_enforced_and_returns_retry_after(self, client):
        # The `login` policy allows 10 per 15 minutes.
        statuses = [(await client.post("/v1/auth/login", json={})).status_code for _ in range(13)]
        assert 429 in statuses
        blocked = await client.post("/v1/auth/login", json={})
        assert blocked.status_code == 429
        assert blocked.json()["error"]["code"] == "rate_limited"
        assert "Retry-After" in blocked.headers


class TestUpstreamFailures:
    async def test_unreachable_upstream_is_503_not_500(self, client, app, monkeypatch):
        def failing(request: httpx.Request) -> httpx.Response:
            raise httpx.ConnectError("connection refused")

        pool = app.state.ctx.extras["pool"]
        pool._clients["books"] = httpx.AsyncClient(
            base_url="http://books.test", transport=httpx.MockTransport(failing)
        )

        response = await client.get("/v1/books")
        assert response.status_code == 503
        assert response.json()["error"]["code"] == "service_unavailable"
        # A dead upstream must not leak a traceback.
        assert "ConnectError" not in response.text

    async def test_upstream_timeout_is_504(self, client, app):
        def slow(request: httpx.Request) -> httpx.Response:
            raise httpx.ReadTimeout("too slow")

        pool = app.state.ctx.extras["pool"]
        pool._clients["search"] = httpx.AsyncClient(
            base_url="http://search.test", transport=httpx.MockTransport(slow)
        )

        response = await client.get("/v1/search?q=x")
        assert response.status_code == 504
        assert response.json()["error"]["code"] == "upstream_timeout"

    async def test_upstream_error_status_is_passed_through(self, client, app):
        def erroring(request: httpx.Request) -> httpx.Response:
            return httpx.Response(422, json={"error": {"code": "validation_error"}})

        pool = app.state.ctx.extras["pool"]
        pool._clients["books"] = httpx.AsyncClient(
            base_url="http://books.test", transport=httpx.MockTransport(erroring)
        )
        response = await client.get("/v1/books")
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"


class TestPlatformSurface:
    async def test_health_is_served_locally_not_proxied(self, client, upstream_log):
        before = len(upstream_log)
        response = await client.get("/health")
        assert response.status_code == 200
        assert response.json()["service"] == "gateway"
        assert len(upstream_log) == before

    async def test_metrics_endpoint(self, client):
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert b"knowledgeos_service_info" in response.content

    async def test_readiness_reports_upstreams_without_failing(self, client):
        response = await client.get("/health/ready")
        # Upstreams are registered as optional: one dead service must not pull the
        # whole gateway out of rotation.
        assert response.status_code == 200
        assert any(k.startswith("upstream:") for k in response.json()["dependencies"])

    async def test_aggregated_openapi_merges_upstream_specs(self, client):
        response = await client.get("/openapi.json")
        assert response.status_code == 200
        body = response.json()
        assert body["info"]["title"] == "KnowledgeOS API"
        assert body["paths"]  # at least one upstream contributed

    async def test_docs_page_carries_its_own_csp(self, client):
        response = await client.get("/docs")
        assert response.status_code == 200
        csp = response.headers["Content-Security-Policy"]
        # The JSON API keeps default-src 'none'; only this page may load scripts.
        assert "cdn.jsdelivr.net" in csp
