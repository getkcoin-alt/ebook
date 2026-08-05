"""SQLAlchemy models for the admin service (schema ``admin``).

Two tables, and the restraint is the point. This service assembles views out of what
other services already own; anything it stored a second copy of would be a second copy
that drifts. There is no orders table here, no book table, and no moderation queue —
reviews belong to the books service, and a moderation queue here would be a duplicate
of state that service already has to keep correct.

What genuinely has no other home is **feature flags**. Every service reads them and no
service owns them, so putting them in any one service's schema would make that service
a dependency of every other one for a reason unrelated to its domain.

``flag_audits`` is separate from the flag row because the flag row is current state
and the audit is history. "Who turned payments off, and when, and why" is a question
that arrives long after the flag has been turned back on.
"""

from __future__ import annotations

import uuid

from sqlalchemy import JSON, Boolean, CheckConstraint, Index, Integer, String, Text
from sqlalchemy.dialects.postgresql import JSONB
from sqlalchemy.orm import Mapped, mapped_column

from knowledgeos_core import Base, TimestampMixin, UUIDPrimaryKeyMixin, UUIDType

SCHEMA = "admin"

JSONType = JSON().with_variant(JSONB, "postgresql")


class FeatureFlag(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """One switch, readable by every service."""

    __tablename__ = "feature_flags"
    __table_args__ = (
        # The key is the identifier every service uses, so it is unique and indexed
        # rather than the primary key being the thing callers know.
        Index("uq_feature_flags_key", "key", unique=True),
        CheckConstraint(
            "rollout_percent >= 0 AND rollout_percent <= 100",
            name="ck_feature_flags_rollout",
        ),
        {"schema": SCHEMA},
    )

    key: Mapped[str] = mapped_column(String(80), nullable=False)
    enabled: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False, default="")

    #: 0-100. A flag with a rollout is evaluated per user; one at 100 is simply on.
    rollout_percent: Mapped[int] = mapped_column(Integer, nullable=False, default=100)
    #: Always on for these user ids. How a flag is tested in production without being
    #: turned on for everybody.
    allowlist: Mapped[list] = mapped_column(JSONType, nullable=False, default=list)

    updated_by: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)


class FlagAudit(UUIDPrimaryKeyMixin, TimestampMixin, Base):
    """Every change to a flag, with what it was and what it became.

    Both sides are stored. "Enabled payments" is a much less useful record than
    "rollout went from 5% to 100%", and only the before-and-after distinguishes them.
    """

    __tablename__ = "flag_audits"
    __table_args__ = (
        Index("ix_flag_audits_key_created", "flag_key", "created_at"),
        {"schema": SCHEMA},
    )

    flag_key: Mapped[str] = mapped_column(String(80), nullable=False)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    before: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    after: Mapped[dict] = mapped_column(JSONType, nullable=False, default=dict)
    reason: Mapped[str] = mapped_column(Text, nullable=False, default="")
    #: Null when a flag was seeded on first boot rather than changed by a person.
    actor_id: Mapped[uuid.UUID | None] = mapped_column(UUIDType, nullable=True)
