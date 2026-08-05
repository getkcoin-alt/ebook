"""Create the first superadmin.

Registration only ever produces a `user`, and promoting anyone requires a permission
only a superadmin holds — so the very first one cannot be made through the API. This
script is the way out of that loop, and it is deliberately the *only* one: no seeded
default account, no "if no users exist, the first signup becomes an admin", no
bootstrap endpoint sitting on the public API waiting to be found.

Run it once, against the database, as an operator:

    python -m bootstrap --email you@company.com

Design decisions, each of them a mistake this avoids:

**No default credentials, ever.** `admin@admin.com` / `admin123` shipped as a fallback
is the single most exploited pattern in self-hosted software — every scanner on the
internet tries it within minutes of a host appearing. There is no default here: the
email is required, and the password is either supplied or generated.

**A generated password is printed once and never stored.** Not in the database as
plaintext, not in a file, not in the logs. If it is lost, run the reset flow.

**It refuses to create a second superadmin.** Not because two is wrong, but because
running this twice by accident should not silently mint another account with total
access. `--force` is there when a second one is genuinely wanted.

**The account is created email-verified.** Verification requires a working mail
provider, and the first thing an operator does is set that up — which they cannot do
if they cannot sign in. A bootstrap that depends on the thing it is bootstrapping is
not a bootstrap.

**It talks to the database directly.** An operational tool, not an API client: at the
moment it runs there is no account that could authorise the call it would need to make.
"""

# ruff: noqa: T201 - `print` is this module's interface. It is a CLI, and the one
# thing it exists to communicate is a password an operator reads off the terminal.
# Routing that through the structured logger would put the credential in whatever
# aggregator the logs ship to, which is the opposite of "shown once, stored nowhere".

from __future__ import annotations

import argparse
import asyncio
import secrets
import string
import sys
import uuid
from datetime import UTC, datetime

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from knowledgeos_core.security import hash_password
from models import User
from schemas import validate_password
from settings import settings

#: Ambiguous characters removed. This password gets read off a terminal and typed
#: into a browser at least once, and `l` versus `1` at 2am is a support ticket.
_ALPHABET = (
    "".join(c for c in string.ascii_letters if c not in "lIO")
    + "".join(c for c in string.digits if c not in "01")
    + "!@#$%^&*-_=+"
)

#: Bounded so a policy this alphabet cannot satisfy is an error, not a hang.
_MAX_GENERATION_ATTEMPTS = 100


def generate_password(length: int = 24) -> str:
    """A password long enough that its composition does not matter.

    Length is where the strength is. 24 characters from this alphabet is ~140 bits,
    which is past the point where anything about the character mix is interesting.

    The length is checked against the policy **before** generating, and the retry loop
    is bounded. Both matter: the policy caps passwords at `password_max_length`, so an
    unchecked `length` above it makes every candidate invalid and the loop spins
    forever — a hang rather than an error, in a script somebody runs at 2am.
    """
    if not settings.password_min_length <= length <= settings.password_max_length:
        raise ValueError(
            f"length must be between {settings.password_min_length} and "
            f"{settings.password_max_length}; got {length}."
        )

    last: ValueError | None = None
    for _ in range(_MAX_GENERATION_ATTEMPTS):
        candidate = "".join(secrets.choice(_ALPHABET) for _ in range(length))
        try:
            # Run it through the real policy rather than trusting the generator —
            # `secrets.choice` can produce an all-lowercase run, and a bootstrap
            # password the service would reject on the next change is a trap.
            return validate_password(candidate)
        except ValueError as exc:
            last = exc
    # Unreachable with any sane policy: at 24 characters the odds of failing the
    # two-character-class rule 100 times running are about 1 in 10^180. If it does
    # happen, the policy and this alphabet disagree, and saying so beats spinning.
    raise RuntimeError(
        f"Could not generate a password satisfying the policy in "
        f"{_MAX_GENERATION_ATTEMPTS} attempts (last: {last})."
    )


async def _existing_superadmins(session: AsyncSession) -> int:
    # A JSON containment query would need a dialect-specific operator; the table this
    # runs against has at most a handful of staff rows, so filtering in Python is both
    # simpler and portable.
    rows = (await session.execute(select(User.roles).where(User.deleted_at.is_(None)))).all()
    return sum(1 for (roles,) in rows if "superadmin" in (roles or []))


async def create_superadmin(
    session: AsyncSession,
    *,
    email: str,
    password: str,
    full_name: str | None,
    force: bool,
) -> tuple[User, bool]:
    """Create or promote. Returns ``(user, created)``.

    Promotion is supported on purpose: the common real sequence is that someone signs
    up through the normal flow first, then needs elevating. Making them delete that
    account and start again would be a worse instruction than supporting the case.
    """
    email = email.strip().lower()

    existing_count = await _existing_superadmins(session)
    if existing_count and not force:
        raise SystemExit(
            f"A superadmin already exists ({existing_count} found).\n"
            "Refusing to create another — re-run with --force if that is what you want."
        )

    user = (await session.execute(select(User).where(User.email == email))).scalars().one_or_none()

    if user is not None:
        roles = list(user.roles or [])
        if "superadmin" not in roles:
            roles.append("superadmin")
        user.roles = roles
        user.is_active = True
        user.banned_at = None
        user.ban_reason = None
        if user.email_verified_at is None:
            user.email_verified_at = datetime.now(UTC)
        if password:
            user.password_hash = hash_password(password)
        await session.commit()
        return user, False

    user = User(
        id=uuid.uuid4(),
        email=email,
        password_hash=hash_password(password),
        full_name=full_name or "Platform Owner",
        roles=["superadmin"],
        extra_permissions=[],
        is_active=True,
        # Verified on creation: verification needs a mail provider, and configuring
        # one is the first thing this account exists to do.
        email_verified_at=datetime.now(UTC),
    )
    session.add(user)
    await session.commit()
    return user, True


async def _run(args: argparse.Namespace) -> int:
    if not settings.database_url:
        print("DATABASE_URL is not set.", file=sys.stderr)
        return 2

    password = args.password or generate_password()
    generated = args.password is None
    try:
        validate_password(password)
    except ValueError as exc:
        print(f"Password rejected: {exc}", file=sys.stderr)
        return 2

    engine = create_async_engine(settings.database_url, echo=False)
    factory = async_sessionmaker(bind=engine, expire_on_commit=False)
    try:
        async with factory() as session:
            user, created = await create_superadmin(
                session,
                email=args.email,
                password=password,
                full_name=args.name,
                force=args.force,
            )
            total_users = int((await session.execute(select(func.count(User.id)))).scalar_one())
    finally:
        await engine.dispose()

    action = "Created" if created else "Promoted existing account"
    print(f"\n  {action}: {user.email}")
    print(f"  Roles:  {', '.join(user.roles)}")
    print(f"  Id:     {user.id}")
    if generated:
        print(f"\n  Password: {password}")
        print("\n  This is shown once and is not stored anywhere. Save it now.")
    print(f"\n  {total_users} account(s) in the directory.")
    print("\n  Next: sign in, then enrol 2FA at POST /v1/auth/mfa/enroll.")
    print("  An operator account without a second factor is one phished password")
    print("  away from the entire platform.\n")
    return 0


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m bootstrap",
        description="Create or promote the first superadmin account.",
    )
    parser.add_argument("--email", required=True, help="The operator's email address.")
    parser.add_argument(
        "--password",
        help="Omit to have one generated and printed once. Never passed on a shared shell.",
    )
    parser.add_argument("--name", help="Display name. Defaults to 'Platform Owner'.")
    parser.add_argument(
        "--force",
        action="store_true",
        help="Proceed even though a superadmin already exists.",
    )
    args = parser.parse_args(argv)
    return asyncio.run(_run(args))


if __name__ == "__main__":
    raise SystemExit(main())
