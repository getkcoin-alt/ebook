"""Tests for password hashing, internal request signing and token verification."""

from __future__ import annotations

import time

import pytest

from knowledgeos_core.errors import ForbiddenError
from knowledgeos_core.security import (
    Principal,
    generate_token,
    hash_body,
    hash_password,
    hash_token,
    needs_rehash,
    sign_internal_request,
    verify_internal_request,
    verify_password,
)


class TestPasswordHashing:
    def test_roundtrip(self) -> None:
        hashed = hash_password("correct horse battery staple")
        assert hashed != "correct horse battery staple"
        assert verify_password("correct horse battery staple", hashed)
        assert not verify_password("wrong password", hashed)

    def test_salted_uniquely(self) -> None:
        assert hash_password("same") != hash_password("same")

    def test_long_passwords_keep_full_entropy(self) -> None:
        # bcrypt truncates at 72 bytes. Without pre-hashing these two would collide,
        # meaning either password could unlock the account.
        a, b = "a" * 80 + "AAAA", "a" * 80 + "BBBB"
        assert not verify_password(b, hash_password(a))
        assert verify_password(a, hash_password(a))

    def test_malformed_stored_hash_is_a_failed_login_not_a_crash(self) -> None:
        assert not verify_password("anything", "not-a-bcrypt-hash")
        assert not verify_password("anything", "")

    def test_unicode_passwords(self) -> None:
        pw = "пароль-密码-🔐"
        assert verify_password(pw, hash_password(pw))

    def test_needs_rehash(self) -> None:
        assert not needs_rehash(hash_password("x"))
        assert needs_rehash("$2b$04$abcdefghijklmnopqrstuv")  # cost 4 < current
        assert needs_rehash("garbage")


class TestInternalSigning:
    def test_valid_signature_accepted(self) -> None:
        body = b'{"book_id": "1"}'
        ts, sig = sign_internal_request(
            "s3cret", method="POST", path="/internal/index", body_hash=hash_body(body)
        )
        assert verify_internal_request(
            "s3cret",
            method="POST",
            path="/internal/index",
            timestamp=ts,
            signature=sig,
            body_hash=hash_body(body),
        )

    @pytest.mark.parametrize(
        ("field", "value"),
        [("method", "GET"), ("path", "/internal/other"), ("body_hash", hash_body(b"tampered"))],
    )
    def test_tampering_is_rejected(self, field: str, value: str) -> None:
        base = {"method": "POST", "path": "/internal/index", "body_hash": hash_body(b"{}")}
        ts, sig = sign_internal_request("s3cret", **base)  # type: ignore[arg-type]
        assert not verify_internal_request(
            "s3cret",
            timestamp=ts,
            signature=sig,
            **{**base, field: value},  # type: ignore[arg-type]
        )

    def test_wrong_secret_rejected(self) -> None:
        ts, sig = sign_internal_request("right", method="GET", path="/x")
        assert not verify_internal_request(
            "wrong", method="GET", path="/x", timestamp=ts, signature=sig
        )

    def test_replay_outside_window_rejected(self) -> None:
        old = int(time.time()) - 3600
        ts, sig = sign_internal_request("s3cret", method="GET", path="/x", timestamp=old)
        assert not verify_internal_request(
            "s3cret", method="GET", path="/x", timestamp=ts, signature=sig, max_age=300
        )

    def test_garbage_timestamp_rejected(self) -> None:
        assert not verify_internal_request(
            "s3cret", method="GET", path="/x", timestamp="not-a-number", signature="deadbeef"
        )


class TestPrincipalAuthorisation:
    def test_exact_permission(self) -> None:
        principal = Principal(user_id="u", permissions=["books:read"])
        assert principal.has_permission("books:read")
        assert not principal.has_permission("books:write")

    def test_resource_wildcard(self) -> None:
        principal = Principal(user_id="u", permissions=["books:*"])
        assert principal.has_permission("books:delete")
        assert not principal.has_permission("users:delete")

    def test_superadmin_bypasses_everything(self) -> None:
        assert Principal(user_id="u", roles=["superadmin"]).has_permission("anything:at:all")

    def test_require_permission_raises_forbidden(self) -> None:
        with pytest.raises(ForbiddenError) as exc:
            Principal(user_id="u", permissions=[]).require_permission("books:delete")
        assert exc.value.details["required"] == "books:delete"

    def test_require_role_accepts_any_listed_role(self) -> None:
        Principal(user_id="u", roles=["moderator"]).require_role("admin", "moderator")
        with pytest.raises(ForbiddenError):
            Principal(user_id="u", roles=["user"]).require_role("admin")


class TestOpaqueTokens:
    def test_tokens_are_unique_and_urlsafe(self) -> None:
        tokens = {generate_token() for _ in range(100)}
        assert len(tokens) == 100
        assert all(
            not set(t) - set("abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_")
            for t in tokens
        )

    def test_hash_is_deterministic_and_irreversible(self) -> None:
        token = generate_token()
        assert hash_token(token) == hash_token(token)
        assert token not in hash_token(token)
