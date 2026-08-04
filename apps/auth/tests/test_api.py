"""End-to-end tests through the real HTTP stack.

These exercise the app as a client sees it: cookies, headers, status codes and the
platform error envelope.
"""

from __future__ import annotations

import pytest

from settings import settings

pytestmark = pytest.mark.asyncio

REGISTRATION = {
    "email": "newreader@knowledgeos.dev",
    "password": "A genuinely fine Passphrase 7",
    "full_name": "New Reader",
}


async def _register_and_login(client, email: str = REGISTRATION["email"]):
    await client.post("/v1/auth/register", json={**REGISTRATION, "email": email})
    response = await client.post(
        "/v1/auth/login", json={"email": email, "password": REGISTRATION["password"]}
    )
    return response


class TestRegistration:
    async def test_register_returns_201(self, client):
        response = await client.post("/v1/auth/register", json=REGISTRATION)
        assert response.status_code == 201
        assert "inbox" in response.json()["message"].lower()

    async def test_duplicate_registration_is_indistinguishable(self, client):
        first = await client.post("/v1/auth/register", json=REGISTRATION)
        second = await client.post("/v1/auth/register", json=REGISTRATION)
        # Identical status and body: the endpoint must not confirm the address exists.
        assert first.status_code == second.status_code == 201
        assert first.json() == second.json()

    @pytest.mark.parametrize(
        "password",
        [
            "short",  # under the minimum length
            "password123",  # in the common-password list
            "alllowercaseonly",  # only one character class
        ],
    )
    async def test_weak_passwords_rejected(self, client, password):
        response = await client.post(
            "/v1/auth/register", json={**REGISTRATION, "password": password}
        )
        assert response.status_code == 422
        assert response.json()["error"]["code"] == "validation_error"

    async def test_invalid_email_rejected(self, client):
        response = await client.post(
            "/v1/auth/register", json={**REGISTRATION, "email": "not-an-email"}
        )
        assert response.status_code == 422

    async def test_unknown_field_rejected(self, client):
        # BaseSchema sets extra="forbid", so a typo'd field is a 422 rather than
        # being silently dropped.
        response = await client.post("/v1/auth/register", json={**REGISTRATION, "is_admin": True})
        assert response.status_code == 422


class TestLogin:
    async def test_login_returns_access_token_and_sets_cookies(self, client):
        response = await _register_and_login(client)
        assert response.status_code == 200
        body = response.json()
        assert body["token_type"] == "Bearer"
        assert body["access_token"]
        assert body["expires_in"] == settings.access_token_ttl
        assert body["user"]["email"] == REGISTRATION["email"]

        # The refresh token belongs in an httpOnly cookie, never in the body.
        assert body.get("refresh_token") is None
        assert settings.refresh_cookie_name in response.cookies
        assert settings.csrf_cookie_name in response.cookies

    async def test_refresh_cookie_is_httponly_and_samesite_strict(self, client):
        response = await _register_and_login(client)
        raw = next(
            value
            for key, value in response.headers.multi_items()
            if key.lower() == "set-cookie" and value.startswith(settings.refresh_cookie_name)
        )
        assert "HttpOnly" in raw  # unreachable from JavaScript, so XSS cannot steal it
        assert "SameSite=strict" in raw.replace("SameSite=Strict", "SameSite=strict")
        assert f"Path={settings.cookie_path}" in raw

    async def test_wrong_password_is_401_with_generic_message(self, client):
        await client.post("/v1/auth/register", json=REGISTRATION)
        response = await client.post(
            "/v1/auth/login",
            json={"email": REGISTRATION["email"], "password": "definitely wrong"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Incorrect email or password."

    async def test_unknown_account_gives_the_same_response(self, client):
        response = await client.post(
            "/v1/auth/login",
            json={"email": "ghost@knowledgeos.dev", "password": "definitely wrong"},
        )
        assert response.status_code == 401
        assert response.json()["error"]["message"] == "Incorrect email or password."


class TestRefreshEndpoint:
    async def test_refresh_rotates_and_requires_csrf(self, client):
        login = await _register_and_login(client)
        csrf = login.json()["csrf_token"]

        # Cookie present but no CSRF header: the double-submit check must reject it,
        # otherwise a cross-site page could silently refresh the session.
        without_header = await client.post("/v1/auth/refresh")
        assert without_header.status_code == 401
        assert without_header.json()["error"]["code"] == "csrf_missing"

        refreshed = await client.post("/v1/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert refreshed.status_code == 200
        assert refreshed.json()["access_token"] != login.json()["access_token"]

    async def test_replaying_a_consumed_refresh_kills_the_session(self, client):
        login = await _register_and_login(client)
        csrf = login.json()["csrf_token"]
        original_cookie = client.cookies.get(
            settings.refresh_cookie_name, path=settings.cookie_path
        )

        first = await client.post("/v1/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert first.status_code == 200

        # Replay the token the client already spent — the signature of theft.
        client.cookies.set(settings.refresh_cookie_name, original_cookie, path=settings.cookie_path)
        replay = await client.post("/v1/auth/refresh", headers={"X-CSRF-Token": csrf})
        assert replay.status_code == 401
        assert replay.json()["error"]["code"] == "refresh_token_reused"

    async def test_refresh_without_any_token_is_401(self, client):
        response = await client.post("/v1/auth/refresh")
        assert response.status_code == 401


class TestAuthenticatedEndpoints:
    async def test_me_requires_a_token(self, client):
        response = await client.get("/v1/auth/me")
        assert response.status_code == 401
        assert response.headers["WWW-Authenticate"] == "Bearer"

    async def test_me_returns_the_profile(self, client):
        login = await _register_and_login(client)
        token = login.json()["access_token"]
        response = await client.get("/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
        assert response.status_code == 200
        body = response.json()
        assert body["email"] == REGISTRATION["email"]
        assert "books:read" in body["permissions"]

    async def test_garbage_token_is_401(self, client):
        response = await client.get("/v1/auth/me", headers={"Authorization": "Bearer not.a.jwt"})
        assert response.status_code == 401

    async def test_profile_update(self, client):
        login = await _register_and_login(client)
        token = login.json()["access_token"]
        response = await client.patch(
            "/v1/auth/me",
            json={"full_name": "Renamed Reader"},
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.json()["full_name"] == "Renamed Reader"

    async def test_sessions_listing_flags_the_current_session(self, client):
        login = await _register_and_login(client)
        token = login.json()["access_token"]
        response = await client.get(
            "/v1/auth/sessions", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        items = response.json()["items"]
        assert len(items) == 1
        assert items[0]["current"] is True


class TestPasswordFlows:
    async def test_forgot_password_response_is_identical_for_unknown_addresses(self, client):
        await client.post("/v1/auth/register", json=REGISTRATION)
        known = await client.post("/v1/auth/forgot-password", json={"email": REGISTRATION["email"]})
        unknown = await client.post(
            "/v1/auth/forgot-password", json={"email": "ghost@knowledgeos.dev"}
        )
        assert known.status_code == unknown.status_code == 200
        assert known.json() == unknown.json()

    async def test_reset_with_invalid_token_is_401(self, client):
        response = await client.post(
            "/v1/auth/reset-password",
            json={"token": "x" * 32, "new_password": "a replacement passphrase 1"},
        )
        assert response.status_code == 401


class TestJWKS:
    async def test_jwks_publishes_a_usable_rsa_key(self, client):
        response = await client.get("/.well-known/jwks.json")
        assert response.status_code == 200
        key = response.json()["keys"][0]
        assert key["kty"] == "RSA"
        assert key["alg"] == "RS256"
        assert key["use"] == "sig"
        assert key["kid"] and key["n"] and key["e"]

    async def test_jwks_is_cacheable(self, client):
        response = await client.get("/.well-known/jwks.json")
        assert "max-age" in response.headers.get("cache-control", "")

    async def test_jwks_never_exposes_private_material(self, client):
        body = (await client.get("/.well-known/jwks.json")).text
        # RSA private parameters. Publishing any of them would hand over the ability
        # to mint tokens for the entire platform.
        for private_param in ('"d"', '"p"', '"q"', '"dp"', '"dq"', '"qi"'):
            assert private_param not in body
        assert "PRIVATE KEY" not in body


class TestInternalEndpoints:
    async def test_internal_endpoints_reject_unsigned_calls(self, client):
        # Private-network reachability is not authorisation.
        response = await client.get(f"/internal/users/{'0' * 8}-0000-0000-0000-{'0' * 12}")
        assert response.status_code == 401

    async def test_internal_endpoint_accepts_a_valid_signature(self, client, app):
        from knowledgeos_core.security import hash_body, sign_internal_request

        login = await _register_and_login(client)
        user_id = login.json()["user"]["id"]
        path = f"/internal/users/{user_id}"
        timestamp, signature = sign_internal_request(
            settings.internal_api_secret, method="GET", path=path, body_hash=hash_body(b"")
        )
        response = await client.get(
            path,
            headers={
                "X-Internal-Timestamp": timestamp,
                "X-Internal-Signature": signature,
                "X-Internal-Service": "books",
            },
        )
        assert response.status_code == 200
        body = response.json()
        assert body["id"] == user_id
        # The internal projection must not carry credentials or MFA state.
        assert "password_hash" not in body
        assert "mfa_enabled" not in body

    async def test_tampered_signature_is_rejected(self, client):
        from knowledgeos_core.security import sign_internal_request

        path = "/internal/users/batch"
        timestamp, signature = sign_internal_request(
            settings.internal_api_secret, method="GET", path="/internal/users/other"
        )
        response = await client.post(
            path,
            json={"user_ids": []},
            headers={
                "X-Internal-Timestamp": timestamp,
                "X-Internal-Signature": signature,
                "X-Internal-Service": "books",
            },
        )
        assert response.status_code == 403


class TestOAuth:
    async def test_providers_endpoint_lists_only_configured_providers(self, client):
        response = await client.get("/v1/auth/oauth/providers")
        assert response.status_code == 200
        # Nothing is configured in tests, so the UI should render no OAuth buttons.
        assert response.json()["providers"] == []

    async def test_unconfigured_provider_gives_a_clear_error(self, client):
        response = await client.get("/v1/auth/oauth/google/start")
        assert response.status_code == 400
        assert "not configured" in response.json()["error"]["message"]


class TestPlatformContract:
    async def test_health_and_metrics_are_present(self, client):
        assert (await client.get("/health")).status_code == 200
        assert (await client.get("/metrics")).status_code == 200

    async def test_errors_use_the_platform_envelope(self, client):
        error = (await client.get("/v1/auth/me")).json()["error"]
        assert set(error) >= {"code", "message"}

    async def test_security_headers_applied(self, client):
        headers = (await client.get("/health")).headers
        assert headers["X-Content-Type-Options"] == "nosniff"
        assert headers["X-Frame-Options"] == "DENY"
