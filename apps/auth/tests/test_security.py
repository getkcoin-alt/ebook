"""Security properties: enumeration resistance, lockout, MFA, key handling.

Each test here corresponds to a specific attack. If one starts failing, a real
weakness has been introduced — these are not style checks.
"""

from __future__ import annotations

import time
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import select

from knowledgeos_core import ConflictError, UnauthorizedError
from models import RecoveryCode, User
from tests.conftest import PASSWORD

pytestmark = pytest.mark.asyncio


class TestUserEnumerationResistance:
    async def test_registering_an_existing_address_looks_identical(self, session, services, user):
        # Returning a distinct "already registered" error would turn this endpoint
        # into a membership oracle for any address an attacker cares to try.
        existing, token = await services["accounts"].register(
            session,
            email=user.email,
            password="another valid passphrase 99",
            full_name="Impostor",
            locale=None,
            ip_address="10.0.0.9",
        )
        assert token is None  # no verification email for a duplicate
        assert existing.id == user.id
        assert existing.full_name == "Test Reader"  # the real account is untouched

    async def test_unknown_and_wrong_password_give_the_same_error(self, session, services, user):
        with pytest.raises(UnauthorizedError) as unknown:
            await services["accounts"].authenticate(
                session, email="nobody@knowledgeos.dev", password="whatever123", ip_address=None
            )
        with pytest.raises(UnauthorizedError) as wrong:
            await services["accounts"].authenticate(
                session, email=user.email, password="wrong password here", ip_address=None
            )
        assert unknown.value.message == wrong.value.message
        assert unknown.value.code == wrong.value.code

    async def test_unknown_address_still_costs_a_password_verification(
        self, session, settings, services
    ):
        # Without the dummy hash, an unknown address returns in microseconds while a
        # known one costs a full bcrypt verify — a timing oracle on its own.
        started = time.perf_counter()
        with pytest.raises(UnauthorizedError):
            await services["accounts"].authenticate(
                session, email="ghost@knowledgeos.dev", password="whatever123", ip_address=None
            )
        elapsed = time.perf_counter() - started
        # bcrypt at cost 12 is ~100ms+; anything near zero means the dummy verify
        # was skipped.
        assert elapsed > 0.02, f"unknown-address login returned in {elapsed:.4f}s"

    async def test_password_reset_for_unknown_address_returns_none_quietly(self, session, services):
        assert (
            await services["accounts"].request_password_reset(
                session, email="ghost@knowledgeos.dev", ip_address=None
            )
            is None
        )

    async def test_oauth_only_account_does_not_reveal_itself(self, session, services):
        session.add(
            User(
                email="oauth-only@knowledgeos.dev",
                password_hash=None,
                roles=["user"],
                extra_permissions=[],
                is_active=True,
            )
        )
        await session.commit()
        with pytest.raises(UnauthorizedError) as exc:
            await services["accounts"].authenticate(
                session, email="oauth-only@knowledgeos.dev", password="anything123", ip_address=None
            )
        # "This account has no password" would confirm the address exists.
        assert exc.value.message == "Incorrect email or password."


class TestAccountLockout:
    async def test_account_locks_after_repeated_failures(self, session, settings, services, user):
        for _ in range(settings.login_max_attempts):
            with pytest.raises(UnauthorizedError):
                await services["accounts"].authenticate(
                    session, email=user.email, password="wrong", ip_address="10.0.0.1"
                )
        # The service mutates the session but never commits — that is the request
        # dependency's job in production, so the test has to play that role.
        await session.commit()
        assert user.locked_until is not None
        assert user.failed_login_attempts == settings.login_max_attempts

        # The correct password must not work while locked, or the lockout is
        # decorative and brute force simply continues.
        with pytest.raises(UnauthorizedError) as exc:
            await services["accounts"].authenticate(
                session, email=user.email, password=PASSWORD, ip_address="10.0.0.1"
            )
        assert exc.value.code == "account_locked"

    async def test_successful_login_clears_the_counter(self, session, services, user):
        with pytest.raises(UnauthorizedError):
            await services["accounts"].authenticate(
                session, email=user.email, password="wrong", ip_address=None
            )
        await services["accounts"].authenticate(
            session, email=user.email, password=PASSWORD, ip_address=None
        )
        assert user.failed_login_attempts == 0

    async def test_password_reset_unlocks_the_account(self, session, services, user):
        user.locked_until = datetime.now(UTC) + timedelta(minutes=15)
        user.failed_login_attempts = 9
        await session.commit()

        result = await services["accounts"].request_password_reset(
            session, email=user.email, ip_address=None
        )
        assert result is not None
        _, raw = result
        await session.commit()

        await services["accounts"].reset_password(
            session, raw_token=raw, new_password="a brand new passphrase 7"
        )
        # Leaving them locked out after proving mailbox control helps only an
        # attacker who triggered the lockout deliberately.
        assert user.locked_until is None
        assert user.failed_login_attempts == 0

    async def test_banned_account_cannot_sign_in(self, session, services, user):
        user.banned_at = datetime.now(UTC)
        await session.commit()
        with pytest.raises(UnauthorizedError) as exc:
            await services["accounts"].authenticate(
                session, email=user.email, password=PASSWORD, ip_address=None
            )
        assert exc.value.code == "account_banned"


class TestPasswordReset:
    async def test_token_is_single_use(self, session, services, user):
        result = await services["accounts"].request_password_reset(
            session, email=user.email, ip_address=None
        )
        assert result is not None
        _, raw = result
        await session.commit()

        await services["accounts"].reset_password(
            session, raw_token=raw, new_password="first replacement pass 1"
        )
        await session.commit()

        with pytest.raises(UnauthorizedError):
            await services["accounts"].reset_password(
                session, raw_token=raw, new_password="second replacement pass 2"
            )

    async def test_expired_token_is_rejected(self, session, services, user):
        from models import PasswordResetToken

        result = await services["accounts"].request_password_reset(
            session, email=user.email, ip_address=None
        )
        assert result is not None
        _, raw = result
        row = (await session.execute(select(PasswordResetToken))).scalar_one()
        row.expires_at = datetime.now(UTC) - timedelta(seconds=1)
        await session.commit()

        with pytest.raises(UnauthorizedError):
            await services["accounts"].reset_password(
                session, raw_token=raw, new_password="too late for this one 3"
            )

    async def test_reset_token_is_stored_hashed(self, session, services, user):
        from models import PasswordResetToken

        result = await services["accounts"].request_password_reset(
            session, email=user.email, ip_address=None
        )
        assert result is not None
        _, raw = result
        await session.commit()
        row = (await session.execute(select(PasswordResetToken))).scalar_one()
        # A database dump must not yield working reset links.
        assert row.token_hash != raw
        assert raw not in row.token_hash

    async def test_change_password_rejects_wrong_current(self, session, services, user):
        with pytest.raises(UnauthorizedError):
            await services["accounts"].change_password(
                session, user, current="not the password", new="a fresh passphrase 44"
            )

    async def test_change_password_rejects_reuse(self, session, services, user):
        with pytest.raises(ConflictError):
            await services["accounts"].change_password(
                session, user, current=PASSWORD, new=PASSWORD
            )


class TestEmailVerification:
    async def test_verification_token_is_single_use(self, session, services):
        _created, raw = await services["accounts"].register(
            session,
            email="new@knowledgeos.dev",
            password="a perfectly fine pass 1",
            full_name=None,
            locale=None,
            ip_address=None,
        )
        await session.commit()
        assert raw is not None

        verified = await services["accounts"].verify_email(session, raw)
        assert verified.is_email_verified
        await session.commit()

        with pytest.raises(UnauthorizedError):
            await services["accounts"].verify_email(session, raw)


class TestTwoFactor:
    async def _enrol(self, session, services, user):
        import pyotp

        secret, _uri = await services["mfa"].begin_enrolment(session, user)
        await session.commit()
        codes = await services["mfa"].confirm_enrolment(session, user, pyotp.TOTP(secret).now())
        await session.commit()
        return secret, codes

    async def test_enrolment_requires_a_valid_code(self, session, services, user):
        await services["mfa"].begin_enrolment(session, user)
        await session.commit()
        with pytest.raises(UnauthorizedError):
            await services["mfa"].confirm_enrolment(session, user, "000000")
        # Not enabled until proven — otherwise scanning the QR and stopping would
        # lock the user out of their own account.
        assert user.mfa_enabled is False

    async def test_enrolment_activates_and_returns_recovery_codes(
        self, session, settings, services, user
    ):
        _secret, codes = await self._enrol(session, services, user)
        assert user.mfa_enabled is True
        assert len(codes) == settings.recovery_code_count

    async def test_totp_code_cannot_be_replayed(self, session, services, user):
        import pyotp

        secret, _ = await self._enrol(session, services, user)
        code = pyotp.TOTP(secret).now()
        # The enrolment already consumed the current step, so the same code must not
        # be accepted again inside its 30-second window.
        with pytest.raises(UnauthorizedError):
            await services["mfa"].verify(session, user, code)

    async def test_recovery_code_works_and_is_single_use(self, session, services, user):
        _secret, codes = await self._enrol(session, services, user)
        assert await services["mfa"].verify(session, user, codes[0]) == "recovery_code"
        await session.commit()
        with pytest.raises(UnauthorizedError):
            await services["mfa"].verify(session, user, codes[0])

    async def test_recovery_codes_are_stored_hashed(self, session, services, user):
        _secret, codes = await self._enrol(session, services, user)
        rows = (await session.execute(select(RecoveryCode))).scalars().all()
        stored = {r.code_hash for r in rows}
        assert not stored & set(codes)

    async def test_totp_secret_is_encrypted_at_rest(self, session, services, user):
        from models import TotpSecret

        secret, _ = await self._enrol(session, services, user)
        row = (await session.execute(select(TotpSecret))).scalar_one()
        # A database dump alone must not yield a working second factor.
        assert secret not in row.secret_encrypted

    async def test_disabling_clears_secret_and_codes(self, session, services, user):
        from models import TotpSecret

        await self._enrol(session, services, user)
        await services["mfa"].disable(session, user)
        await session.commit()
        assert user.mfa_enabled is False
        assert (await session.execute(select(TotpSecret))).scalar_one_or_none() is None
        assert not (await session.execute(select(RecoveryCode))).scalars().all()


class TestSigningKeys:
    async def test_production_refuses_to_boot_without_a_key(self, settings):
        from services import KeyRing

        production = settings.model_copy(update={"environment": "production"})
        # An ephemeral key in production would invalidate every session on every
        # restart, and replicas would disagree with each other.
        with pytest.raises(RuntimeError, match="JWT_PRIVATE_KEY is required"):
            KeyRing(production).load()

    async def test_mismatched_keypair_is_rejected_at_boot(self, settings):
        from services import KeyRing, generate_keypair

        private, _ = generate_keypair()
        _, other_public = generate_keypair()
        broken = settings.model_copy(
            update={"jwt_private_key": private, "jwt_public_key": other_public}
        )
        with pytest.raises(RuntimeError, match="does not match"):
            KeyRing(broken).load()

    async def test_previous_key_is_published_for_rotation(self, settings):
        from services import KeyRing, generate_keypair

        private, public = generate_keypair()
        _, retiring = generate_keypair()
        ring = KeyRing(
            settings.model_copy(
                update={
                    "jwt_private_key": private,
                    "jwt_public_key": public,
                    "jwt_previous_public_key": retiring,
                }
            )
        )
        ring.load()
        jwks = ring.jwks()
        # Both keys published, so tokens signed before the swap keep verifying.
        assert len(jwks["keys"]) == 2
        assert ring.active.kid == jwks["keys"][0]["kid"]

    async def test_kid_is_derived_from_the_key_material(self, settings):
        from services import KeyRing, generate_keypair

        private, public = generate_keypair()
        overrides = {"jwt_private_key": private, "jwt_public_key": public}
        first = KeyRing(settings.model_copy(update=overrides))
        second = KeyRing(settings.model_copy(update=overrides))
        first.load()
        second.load()
        # Replicas must agree on the kid without coordinating.
        assert first.active.kid == second.active.kid

    async def test_undersized_key_is_rejected(self, settings):
        from cryptography.hazmat.primitives import serialization
        from cryptography.hazmat.primitives.asymmetric import rsa

        from services import KeyRing

        weak = rsa.generate_private_key(public_exponent=65537, key_size=1024)
        pem = weak.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        ).decode()
        with pytest.raises(RuntimeError, match="2048 is the minimum"):
            KeyRing(settings.model_copy(update={"jwt_private_key": pem})).load()
