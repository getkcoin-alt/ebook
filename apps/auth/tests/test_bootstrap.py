"""The first-superadmin bootstrap.

Registration only ever produces a `user`, and promotion needs a permission only a
superadmin holds — so the very first one cannot be made through the API. These tests
cover the escape from that loop, and the guards that keep it from becoming a back door.
"""

from __future__ import annotations

import pytest
from bootstrap import create_superadmin, generate_password
from sqlalchemy import select

from models import User
from schemas import validate_password


class TestGeneratedPassword:
    def test_it_satisfies_the_real_policy(self):
        """Not the generator's own idea of strong — `secrets.choice` can produce an
        all-lowercase run, and a bootstrap password the service would reject on the
        next change is a trap."""
        for _ in range(50):
            assert validate_password(generate_password()) is not None

    def test_it_avoids_ambiguous_characters(self):
        """This gets read off a terminal and typed into a browser at least once, and
        `l` versus `1` at 2am is a support ticket."""
        sample = "".join(generate_password(64) for _ in range(20))
        for ambiguous in "lIO01":
            assert ambiguous not in sample

    def test_a_length_the_policy_forbids_raises_rather_than_hanging(self):
        """The policy caps passwords, so an unchecked length above it makes every
        candidate invalid and the retry loop spins forever — a hang rather than an
        error, in a script somebody runs at 2am."""
        with pytest.raises(ValueError, match="length must be between"):
            generate_password(500)

        with pytest.raises(ValueError, match="length must be between"):
            generate_password(4)

    def test_two_calls_never_agree(self):
        assert generate_password() != generate_password()


class TestCreate:
    async def test_it_creates_a_verified_superadmin(self, session, settings):
        """Verification needs a mail provider, and configuring one is the first thing
        this account exists to do — a bootstrap that depends on the thing it is
        bootstrapping is not a bootstrap."""
        user, created = await create_superadmin(
            session,
            email="Owner@Example.COM",
            password="correct horse battery Staple 9",
            full_name="Platform Owner",
            force=False,
        )

        assert created is True
        assert user.roles == ["superadmin"]
        assert user.email_verified_at is not None
        assert user.is_active is True
        # Normalised, so a later sign-in with different casing finds the same row.
        assert user.email == "owner@example.com"

    async def test_the_password_is_hashed_not_stored(self, session, settings):
        from knowledgeos_core.security import verify_password

        secret = "correct horse battery Staple 9"
        user, _ = await create_superadmin(
            session, email="o@e.dev", password=secret, full_name=None, force=False
        )

        assert user.password_hash != secret
        assert verify_password(secret, user.password_hash)

    async def test_it_refuses_a_second_superadmin(self, session, settings):
        """Running this twice by accident should not silently mint another account
        with total access."""
        await create_superadmin(
            session, email="first@e.dev", password="Passw0rd for first", full_name=None, force=False
        )

        with pytest.raises(SystemExit, match="already exists"):
            await create_superadmin(
                session,
                email="second@e.dev",
                password="Passw0rd for second",
                full_name=None,
                force=False,
            )

    async def test_force_allows_a_second_one(self, session, settings):
        await create_superadmin(
            session, email="first@e.dev", password="Passw0rd for first", full_name=None, force=False
        )
        _user, created = await create_superadmin(
            session,
            email="second@e.dev",
            password="Passw0rd for second",
            full_name=None,
            force=True,
        )

        assert created is True
        assert len((await session.execute(select(User))).scalars().all()) == 2


class TestPromote:
    async def test_an_existing_account_is_promoted_not_duplicated(self, session, settings, user):
        """The common real sequence is that someone signs up normally first, then
        needs elevating. Making them delete and start again would be worse advice."""
        promoted, created = await create_superadmin(
            session,
            email=user.email,
            password="A new Passw0rd here",
            full_name=None,
            force=False,
        )

        assert created is False
        assert promoted.id == user.id
        assert "superadmin" in promoted.roles

        rows = (await session.execute(select(User).where(User.email == user.email))).scalars().all()
        assert len(rows) == 1

    async def test_promotion_keeps_existing_roles(self, session, settings, user):
        user.roles = ["author"]
        await session.commit()

        promoted, _ = await create_superadmin(
            session, email=user.email, password="A new Passw0rd here", full_name=None, force=False
        )

        assert set(promoted.roles) == {"author", "superadmin"}

    async def test_promotion_lifts_a_ban(self, session, settings, user):
        """Otherwise the one account that can unban is the one that is banned."""
        from datetime import UTC, datetime

        user.banned_at = datetime.now(UTC)
        user.ban_reason = "mistake"
        await session.commit()

        promoted, _ = await create_superadmin(
            session, email=user.email, password="A new Passw0rd here", full_name=None, force=False
        )

        assert promoted.banned_at is None
        assert promoted.ban_reason is None


class TestTheAccountActuallyWorks:
    async def test_it_can_sign_in_and_holds_every_permission(self, client, session, settings):
        """The end-to-end assertion: the bootstrapped account must be able to log in
        through the ordinary endpoint and come back with an admin-capable token."""
        secret = "correct horse battery Staple 9"
        await create_superadmin(
            session, email="owner@e.dev", password=secret, full_name=None, force=False
        )

        response = await client.post(
            "/v1/auth/login", json={"email": "owner@e.dev", "password": secret}
        )
        assert response.status_code == 200

        body = response.json()
        permissions = set(body["user"]["permissions"])
        # superadmin resolves to every permission, which is what makes the console
        # usable on the first sign-in.
        assert {"users:read", "users:write", "settings:write", "analytics:read"} <= permissions

    async def test_the_bootstrapped_account_can_reach_the_admin_directory(
        self, client, session, settings
    ):
        secret = "correct horse battery Staple 9"
        await create_superadmin(
            session, email="owner@e.dev", password=secret, full_name=None, force=False
        )
        login = await client.post(
            "/v1/auth/login", json={"email": "owner@e.dev", "password": secret}
        )
        headers = {"Authorization": f"Bearer {login.json()['access_token']}"}

        listed = await client.get("/v1/admin/users", headers=headers)
        assert listed.status_code == 200
        assert listed.json()["total"] >= 1


class TestCommandLine:
    """`main()` is the actual entry point — arg parsing, password generation, the
    printed summary and the exit code. The functions under it were already covered;
    this covers the wrapper an operator actually runs."""

    @pytest.fixture(autouse=True)
    def _use_the_test_engine(self, engine, monkeypatch, settings):
        """Point the CLI at the fixture engine.

        `_run` builds its own engine from DATABASE_URL because at the moment it runs
        there is no application context to borrow one from. Redirecting it here is the
        only way to exercise that path without a live database.
        """
        import bootstrap

        class _KeepAlive:
            """The fixture engine, with disposal disarmed.

            `AsyncEngine.dispose` is read-only, so it is wrapped rather than patched.
            The engine fixture owns teardown; letting the CLI dispose it would drop
            the in-memory database before the assertions run.
            """

            def __init__(self, inner) -> None:  # type: ignore[no-untyped-def]
                self._inner = inner

            def __getattr__(self, name: str):  # type: ignore[no-untyped-def]
                return getattr(self._inner, name)

            async def dispose(self) -> None:
                return None

        monkeypatch.setattr(bootstrap, "create_async_engine", lambda *a, **k: _KeepAlive(engine))

    def test_it_creates_an_account_and_prints_the_password_once(self, capsys):
        import bootstrap

        assert bootstrap.main(["--email", "owner@e.dev", "--name", "Owner"]) == 0

        out = capsys.readouterr().out
        assert "Created" in out
        assert "owner@e.dev" in out
        assert "superadmin" in out
        assert "Password:" in out
        # The reminder matters more than it looks: this is the only time it exists.
        assert "shown once" in out

    def test_a_supplied_password_is_never_printed(self, capsys):
        """Echoing it back would put it in the terminal scrollback and the CI log of
        anyone who scripts this."""
        import bootstrap

        secret = "correct horse battery Staple 9"
        assert bootstrap.main(["--email", "owner@e.dev", "--password", secret]) == 0
        assert secret not in capsys.readouterr().out

    def test_a_weak_password_is_rejected_before_anything_is_written(self, capsys):
        import bootstrap

        assert bootstrap.main(["--email", "owner@e.dev", "--password", "short"]) == 2
        assert "Password rejected" in capsys.readouterr().err

    def test_running_twice_refuses_the_second(self, capsys):
        """Running this twice by accident should not silently mint another account
        with total access."""
        import bootstrap

        assert bootstrap.main(["--email", "first@e.dev"]) == 0
        with pytest.raises(SystemExit, match="already exists"):
            bootstrap.main(["--email", "second@e.dev"])

    def test_force_allows_the_second(self, capsys):
        import bootstrap

        assert bootstrap.main(["--email", "first@e.dev"]) == 0
        assert bootstrap.main(["--email", "second@e.dev", "--force"]) == 0

    def test_email_is_required(self):
        import bootstrap

        with pytest.raises(SystemExit):
            bootstrap.main([])
