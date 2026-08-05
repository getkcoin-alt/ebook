"""Feature flag administration and the internal read path every service uses."""

from __future__ import annotations

import uuid
from typing import Annotated

from fastapi import APIRouter, Depends, Query, status

from deps import Flags, PageOffset
from knowledgeos_core import MessageResponse, get_logger
from knowledgeos_core.deps import CurrentUser, DbSession, InternalCaller, require_permission
from schemas import (
    FlagAuditOut,
    FlagAuditPage,
    FlagBatchRequest,
    FlagBatchResponse,
    FlagCreate,
    FlagEvaluation,
    FlagOut,
    FlagUpdate,
)

logger = get_logger(__name__)

SETTINGS_WRITE = Depends(require_permission("settings:write"))
ANALYTICS_READ = Depends(require_permission("analytics:read"))

router = APIRouter(prefix="/v1/admin/flags", tags=["admin"])
internal_router = APIRouter(prefix="/internal", tags=["internal"])


@router.get(
    "",
    response_model=list[FlagOut],
    summary="Every feature flag",
    dependencies=[ANALYTICS_READ],
)
async def list_flags(session: DbSession, flags: Flags) -> list[FlagOut]:
    return [FlagOut.model_validate(flag) for flag in await flags.list_flags(session)]


@router.post(
    "",
    response_model=FlagOut,
    status_code=status.HTTP_201_CREATED,
    summary="Create a flag",
    description=(
        "Keys are lower-case letters, digits, dot, dash and underscore. Constrained "
        "because the key is embedded in cache keys and read by every service — one "
        "with a colon or a space in it produces a cache collision that is very hard "
        "to see."
    ),
    dependencies=[SETTINGS_WRITE],
)
async def create_flag(
    payload: FlagCreate, session: DbSession, flags: Flags, user: CurrentUser
) -> FlagOut:
    flag = await flags.create(session, payload, actor_id=uuid.UUID(user.user_id))
    return FlagOut.model_validate(flag)


@router.get(
    "/{key}",
    response_model=FlagOut,
    summary="One flag",
    dependencies=[ANALYTICS_READ],
)
async def get_flag(key: str, session: DbSession, flags: Flags) -> FlagOut:
    return FlagOut.model_validate(await flags.get(session, key))


@router.patch(
    "/{key}",
    response_model=FlagOut,
    summary="Change a flag",
    description=(
        "Partial. Every change is audited with **both** the before and the after — "
        '"enabled payments" is a far less useful record than "rollout went from 5% '
        'to 100%", and only before-and-after separates them.\n\n'
        "Raising a rollout only ever adds users. Bucketing is a stable hash of the "
        "flag key and the user id, so nobody who had the feature loses it — a rollout "
        "that reshuffles reads to a user as a bug in the feature."
    ),
    dependencies=[SETTINGS_WRITE],
)
async def update_flag(
    key: str, payload: FlagUpdate, session: DbSession, flags: Flags, user: CurrentUser
) -> FlagOut:
    flag = await flags.update(session, key, payload, actor_id=uuid.UUID(user.user_id))
    return FlagOut.model_validate(flag)


@router.delete(
    "/{key}",
    response_model=MessageResponse,
    summary="Delete a flag",
    description=(
        "Once the code reading it is gone. The audit history survives the flag — "
        "deleting that too would lose the record of a flag that was on during an "
        "incident."
    ),
    dependencies=[SETTINGS_WRITE],
)
async def delete_flag(
    key: str,
    session: DbSession,
    flags: Flags,
    user: CurrentUser,
    reason: Annotated[str, Query(max_length=500)] = "",
) -> MessageResponse:
    await flags.delete(session, key, actor_id=uuid.UUID(user.user_id), reason=reason)
    return MessageResponse(message="Flag deleted.")


@router.get(
    "/{key}/audit",
    response_model=FlagAuditPage,
    summary="A flag's change history",
    dependencies=[ANALYTICS_READ],
)
async def flag_audit(key: str, session: DbSession, flags: Flags, page: PageOffset) -> FlagAuditPage:
    limit, offset = page
    rows, total = await flags.audits(session, limit=limit, offset=offset, key=key)
    return FlagAuditPage(
        items=[FlagAuditOut.model_validate(row) for row in rows],
        total=total,
        limit=limit,
        offset=offset,
    )


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


@internal_router.post(
    "/flags/evaluate",
    response_model=FlagBatchResponse,
    summary="Evaluate flags for a user (internal)",
    description=(
        "How every other service reads flags. Returns **decisions, not rules**: "
        "handing back a rollout percentage would make each service implement the "
        "bucketing itself, and two implementations of a hash bucket diverge — which "
        "shows up as a user who has a feature on one page and not the next.\n\n"
        "Unknown keys are reported *and* answered `false`. Reporting them makes a "
        'typo in a service\'s flag name visible instead of looking like "off"; '
        "answering them keeps a caller that only reads `flags` from raising."
    ),
)
async def evaluate_flags(
    payload: FlagBatchRequest,
    caller: InternalCaller,
    session: DbSession,
    flags: Flags,
) -> FlagBatchResponse:
    decisions, unknown = await flags.evaluate_many(session, payload.keys, user_id=payload.user_id)
    if unknown:
        logger.info("admin.unknown_flags_requested", caller=caller, keys=unknown)
    return FlagBatchResponse(flags=decisions, unknown=unknown)


@internal_router.get(
    "/flags/{key}",
    response_model=FlagEvaluation,
    summary="Evaluate one flag (internal)",
    description=(
        "The `reason` field explains the answer — allowlisted, disabled, which "
        'bucket. It exists for the conversation that starts "the flag is not '
        'working", which is otherwise unanswerable from either side.'
    ),
)
async def evaluate_flag(
    key: str,
    caller: InternalCaller,
    session: DbSession,
    flags: Flags,
    user_id: Annotated[uuid.UUID | None, Query()] = None,
) -> FlagEvaluation:
    return await flags.evaluate(session, key, user_id=user_id)


@internal_router.post(
    "/maintenance/prune-audits",
    response_model=MessageResponse,
    summary="Drop flag audit rows past retention (internal)",
)
async def prune_audits(caller: InternalCaller, session: DbSession, flags: Flags) -> MessageResponse:
    return MessageResponse(message=f"Pruned {await flags.prune_audits(session)} audit rows.")
