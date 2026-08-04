"""Token issuance, rotation and reuse detection.

The reuse-detection tests are the most important in the service: they are what stops
a stolen refresh token from being a silent 30-day credential.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from jose import jwt
from sqlalchemy import select

from knowledgeos_core import UnauthorizedError
from models import RefreshToken, Session

pytestmark = pytest.mark.asyncio


async def _login(session, services, user):
    return await services["tokens"].start_session(
        session,
        user,
        ip_address="10.0.0.1",
        user_agent="pytest",
        device_label="laptop",
        auth_method="password",
    )


class TestAccessTokens:
    async def test_claims_are_complete_and_correct(self, session, services, user, keyring):
        issued = await _login(session, services, user)
        claims = jwt.decode(
            issued.access_token,
            keyring.active.public_pem,
            algorithms=["RS256"],
            audience="knowledgeos-api",
            issuer="knowledgeos-auth",
        )
        assert claims["sub"] == str(user.id)
        assert claims["typ"] == "access"
        assert claims["email"] == user.email
        assert "books:read" in claims["permissions"]
        assert claims["sid"] == str(issued.session_id)
        assert claims["jti"]

    async def test_header_carries_kid_for_rotation(self, session, services, user, keyring):
        issued = await _login(session, services, user)
        header = jwt.get_unverified_header(issued.access_token)
        assert header["kid"] == keyring.active.kid
        assert header["alg"] == "RS256"

    async def test_permissions_follow_roles(self, session, services, user):
        user.roles = ["admin"]
        issued = await _login(session, services, user)
        claims = jwt.get_unverified_claims(issued.access_token)
        assert "books:delete" in claims["permissions"]
        assert "analytics:read" in claims["permissions"]

    async def test_unknown_role_is_ignored_not_fatal(self, session, services, user):
        # A role removed from the enum must not break every login for users who
        # still carry it in their row.
        user.roles = ["user", "wizard"]
        issued = await _login(session, services, user)
        claims = jwt.get_unverified_claims(issued.access_token)
        assert "books:read" in claims["permissions"]


class TestRefreshRotation:
    async def test_rotation_issues_a_new_token_and_consumes_the_old(self, session, services, user):
        first = await _login(session, services, user)
        await session.commit()

        second = await services["tokens"].rotate(
            session, raw_refresh=first.refresh_token, csrf_token=first.csrf_token
        )
        assert second.refresh_token != first.refresh_token
        assert second.user_id == user.id
        # Same session and family: rotation continues a login, it does not start one.
        assert second.session_id == first.session_id

    async def test_reuse_of_a_consumed_token_revokes_the_whole_family(
        self, session, services, user
    ):
        first = await _login(session, services, user)
        await session.commit()

        second = await services["tokens"].rotate(
            session, raw_refresh=first.refresh_token, csrf_token=first.csrf_token
        )
        await session.commit()

        # The attacker replays the token the legitimate client already spent.
        with pytest.raises(UnauthorizedError) as exc:
            await services["tokens"].rotate(
                session, raw_refresh=first.refresh_token, csrf_token=first.csrf_token
            )
        assert exc.value.code == "refresh_token_reused"
        await session.commit()

        # The victim's current token must also be dead — otherwise the attacker has
        # simply been logged out and the victim carries on unaware.
        with pytest.raises(UnauthorizedError):
            await services["tokens"].rotate(
                session, raw_refresh=second.refresh_token, csrf_token=second.csrf_token
            )

        rows = (
            (await session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id)))
            .scalars()
            .all()
        )
        assert all(r.revoked_at is not None for r in rows)
        assert {r.revoked_reason for r in rows} == {"reuse_detected"}

    async def test_unknown_token_is_rejected(self, session, services, user):
        with pytest.raises(UnauthorizedError):
            await services["tokens"].rotate(
                session, raw_refresh="not-a-real-token", csrf_token=None
            )

    async def test_expired_token_is_rejected(self, session, services, user):
        issued = await _login(session, services, user)
        row = (
            await session.execute(
                select(RefreshToken).where(RefreshToken.session_id == issued.session_id)
            )
        ).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

        with pytest.raises(UnauthorizedError, match="expired"):
            await services["tokens"].rotate(
                session, raw_refresh=issued.refresh_token, csrf_token=issued.csrf_token
            )

    async def test_wrong_csrf_token_is_rejected(self, session, services, user):
        issued = await _login(session, services, user)
        await session.commit()
        with pytest.raises(UnauthorizedError) as exc:
            await services["tokens"].rotate(
                session, raw_refresh=issued.refresh_token, csrf_token="wrong-value"
            )
        assert exc.value.code == "csrf_mismatch"

    async def test_revoked_session_cannot_refresh(self, session, services, user):
        issued = await _login(session, services, user)
        await session.commit()
        await services["tokens"].revoke_session(session, issued.session_id, reason="logout")
        await session.commit()

        with pytest.raises(UnauthorizedError):
            await services["tokens"].rotate(
                session, raw_refresh=issued.refresh_token, csrf_token=issued.csrf_token
            )

    async def test_banned_user_cannot_refresh(self, session, services, user):
        issued = await _login(session, services, user)
        user.banned_at = datetime.now(UTC)
        await session.commit()

        with pytest.raises(UnauthorizedError) as exc:
            await services["tokens"].rotate(
                session, raw_refresh=issued.refresh_token, csrf_token=issued.csrf_token
            )
        assert exc.value.code == "account_banned"

    async def test_separate_logins_get_separate_families(self, session, services, user):
        # Revoking a compromised chain must not sign the user out on their phone.
        first = await _login(session, services, user)
        second = await _login(session, services, user)
        await session.commit()

        rows = {
            r.session_id: r.family_id
            for r in (
                await session.execute(select(RefreshToken).where(RefreshToken.user_id == user.id))
            )
            .scalars()
            .all()
        }
        assert rows[first.session_id] != rows[second.session_id]


class TestRevocation:
    async def test_revoke_all_sessions(self, session, services, user):
        await _login(session, services, user)
        await _login(session, services, user)
        await session.commit()

        await services["tokens"].revoke_all_sessions(session, user.id, reason="password_change")
        await session.commit()

        sessions = (
            (await session.execute(select(Session).where(Session.user_id == user.id)))
            .scalars()
            .all()
        )
        assert sessions and all(s.revoked_at is not None for s in sessions)


class TestMfaChallenge:
    async def test_challenge_roundtrip(self, services, user):
        token, ttl = services["tokens"].mint_mfa_challenge(user.id)
        assert ttl > 0
        assert services["tokens"].read_mfa_challenge(token) == user.id

    async def test_challenge_cannot_be_used_as_an_access_token(self, services, user, keyring):
        # Signed by the same key, so only the `typ` claim separates them. Confusing
        # the two would let a half-authenticated user skip the second factor.
        token, _ = services["tokens"].mint_mfa_challenge(user.id)
        claims = jwt.get_unverified_claims(token)
        assert claims["typ"] == "mfa_challenge"
        assert "permissions" not in claims
        assert "roles" not in claims

    async def test_access_token_is_not_accepted_as_a_challenge(self, session, services, user):
        issued = await _login(session, services, user)
        with pytest.raises(UnauthorizedError):
            services["tokens"].read_mfa_challenge(issued.access_token)

    async def test_garbage_challenge_rejected(self, services):
        with pytest.raises(UnauthorizedError):
            services["tokens"].read_mfa_challenge("not.a.token")
