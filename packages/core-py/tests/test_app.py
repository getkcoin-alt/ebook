"""Contract tests for the application factory.

These lock down the guarantees every service inherits. A regression here breaks all
eleven services at once, so the assertions are deliberately strict.
"""

from __future__ import annotations

import pytest
from httpx import AsyncClient

pytestmark = pytest.mark.asyncio


class TestHealthEndpoints:
    async def test_health_is_dependency_free(self, client: AsyncClient) -> None:
        response = await client.get("/health")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert body["service"] == "test-svc"
        assert body["version"] == "9.9.9"
        assert body["uptime_seconds"] >= 0

    async def test_ready_reports_dependencies(self, client: AsyncClient) -> None:
        response = await client.get("/health/ready")
        assert response.status_code == 200
        assert response.json()["status"] == "ready"

    async def test_startup_probe(self, client: AsyncClient) -> None:
        assert (await client.get("/health/startup")).status_code == 200

    async def test_ready_fails_while_draining(self, client: AsyncClient, app) -> None:
        # Readiness must flip to 503 before shutdown so load balancers drain the
        # instance instead of sending requests into a closing process.
        app.state.ctx.health.begin_shutdown()
        response = await client.get("/health/ready")
        assert response.status_code == 503
        assert response.json()["status"] == "draining"


class TestMetrics:
    async def test_metrics_exposes_prometheus_text(self, client: AsyncClient) -> None:
        response = await client.get("/metrics")
        assert response.status_code == 200
        assert "text/plain" in response.headers["content-type"]
        assert b"knowledgeos_service_info" in response.content

    async def test_request_counter_uses_route_template_not_raw_path(
        self, client: AsyncClient
    ) -> None:
        # Cardinality guard: a crawler hitting thousands of distinct URLs must not
        # create thousands of Prometheus series.
        await client.get("/v1/public")
        body = (await client.get("/metrics")).text
        assert 'path="/v1/public"' in body

    async def test_probes_are_not_counted(self, client: AsyncClient) -> None:
        await client.get("/health")
        body = (await client.get("/metrics")).text
        assert 'path="/health"' not in body


class TestErrorEnvelope:
    async def test_unhandled_exception_does_not_leak_internals(self, client: AsyncClient) -> None:
        response = await client.get("/v1/boom")
        assert response.status_code == 500
        assert "postgres://" not in response.text
        assert "leaky detail" not in response.text
        assert response.json()["error"]["code"] == "internal_error"

    async def test_404_uses_platform_envelope(self, client: AsyncClient) -> None:
        response = await client.get("/v1/does-not-exist")
        assert response.status_code == 404
        assert response.json()["error"]["code"] == "resource_not_found"

    async def test_validation_error_lists_fields(self, client: AsyncClient) -> None:
        response = await client.post("/v1/echo", json={"a": ["not", "a", "string"]})
        assert response.status_code == 422
        error = response.json()["error"]
        assert error["code"] == "validation_error"
        assert "fields" in error["details"]

    async def test_errors_carry_the_request_id(self, client: AsyncClient) -> None:
        response = await client.get("/v1/does-not-exist")
        assert response.json()["error"]["request_id"] == response.headers["X-Request-ID"]


class TestAuth:
    async def test_missing_token_is_401(self, client: AsyncClient) -> None:
        response = await client.get("/v1/private")
        assert response.status_code == 401
        assert response.json()["error"]["code"] == "unauthorized"
        assert response.headers["WWW-Authenticate"] == "Bearer"

    async def test_optional_auth_allows_anonymous(self, client: AsyncClient) -> None:
        response = await client.get("/v1/maybe")
        assert response.status_code == 200
        assert response.json()["user"] is None

    async def test_dependency_override_authenticates(self, client: AsyncClient, app) -> None:
        from knowledgeos_core.testing import authenticate_as, make_principal

        principal = make_principal(user_id="user-123", roles=["admin"])
        authenticate_as(app, principal)
        try:
            response = await client.get("/v1/private")
            assert response.status_code == 200
            assert response.json()["user"] == "user-123"
        finally:
            app.dependency_overrides.clear()


class TestSecurityHeaders:
    async def test_baseline_headers_present(self, client: AsyncClient) -> None:
        headers = (await client.get("/v1/public")).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
        assert headers["Referrer-Policy"] == "strict-origin-when-cross-origin"
        assert "frame-ancestors 'none'" in headers["Content-Security-Policy"]

    async def test_hsts_only_in_production(self, client: AsyncClient) -> None:
        assert "Strict-Transport-Security" in (await client.get("/v1/public")).headers

    async def test_server_version_not_advertised(self, client: AsyncClient) -> None:
        assert "server" not in {k.lower() for k in (await client.get("/v1/public")).headers}


class TestRequestCorrelation:
    async def test_request_id_generated_when_absent(self, client: AsyncClient) -> None:
        request_id = (await client.get("/v1/public")).headers["X-Request-ID"]
        assert request_id and len(request_id) == 32

    async def test_inbound_request_id_is_propagated(self, client: AsyncClient) -> None:
        response = await client.get("/v1/public", headers={"X-Request-ID": "abc-123"})
        assert response.headers["X-Request-ID"] == "abc-123"

    async def test_oversized_request_id_is_truncated(self, client: AsyncClient) -> None:
        # An unbounded client-supplied id would be written straight into the log index.
        response = await client.get("/v1/public", headers={"X-Request-ID": "x" * 500})
        assert len(response.headers["X-Request-ID"]) == 64


class TestDocsExposure:
    async def test_docs_are_closed_in_production(self, client: AsyncClient) -> None:
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/openapi.json")).status_code == 404

    async def test_docs_open_outside_production(self, router) -> None:
        from asgi_lifespan import LifespanManager
        from httpx import ASGITransport

        from knowledgeos_core import Components, ServiceSettings, create_app

        app = create_app(
            settings=ServiceSettings(service_name="dev-svc", environment="local"),
            components=Components(),
            routers=[router],
        )
        async with (
            LifespanManager(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as dev_client,
        ):
            assert (await dev_client.get("/openapi.json")).status_code == 200


class TestBodyLimit:
    async def test_oversized_body_is_rejected(self, router) -> None:
        from asgi_lifespan import LifespanManager
        from httpx import ASGITransport

        from knowledgeos_core import Components, ServiceSettings, create_app

        app = create_app(
            settings=ServiceSettings(
                service_name="tiny", environment="local", max_request_body_bytes=100
            ),
            components=Components(),
            routers=[router],
        )
        async with (
            LifespanManager(app),
            AsyncClient(transport=ASGITransport(app=app), base_url="http://test") as tiny_client,
        ):
            response = await tiny_client.post("/v1/echo", json={"k": "v" * 500})
            assert response.status_code == 413
            assert response.json()["error"]["code"] == "payload_too_large"
