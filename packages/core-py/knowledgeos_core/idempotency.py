"""Idempotency keys for unsafe operations.

Contract (the same one Stripe uses): a client sends ``Idempotency-Key: <uuid>`` on a
POST. The first request executes and its response is stored. Any replay of the same
key returns the stored response without re-executing.

This closes three real failure modes: the user double-clicking Buy, a mobile client
retrying after a timeout on a request the server actually completed, and a payment
provider redelivering a webhook.

Two details that make it correct rather than merely present:

1. A key is *reserved* atomically (``SET NX``) before the handler runs, so two
   concurrent replays cannot both execute. The second gets 409 rather than a
   duplicate charge.
2. The request fingerprint is stored with the key. Reusing one key with a different
   body is a client bug and is rejected instead of silently returning the wrong
   cached response.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from typing import Any

import orjson
from redis.asyncio.client import Redis

from .errors import ConflictError
from .logging import get_logger

logger = get_logger(__name__)

DEFAULT_TTL = 86_400  # 24h, matching common payment-provider replay windows


@dataclass(slots=True)
class IdempotentReplay:
    """A stored response for a previously completed request."""

    status_code: int
    body: Any
    headers: dict[str, str]


class IdempotencyStore:
    def __init__(self, redis: Redis, *, service_name: str, ttl: int = DEFAULT_TTL) -> None:
        self._redis = redis
        self._service = service_name
        self._ttl = ttl

    def _key(self, scope: str, key: str, actor: str) -> str:
        # Scoping by actor stops one tenant from probing or colliding with another's
        # keys, which would otherwise leak a response body across accounts.
        return f"kos:idem:{self._service}:{scope}:{actor}:{key}"

    @staticmethod
    def fingerprint(payload: Any) -> str:
        return hashlib.sha256(orjson.dumps(payload, option=orjson.OPT_SORT_KEYS)).hexdigest()

    async def begin(
        self, *, scope: str, key: str, actor: str, request_fingerprint: str
    ) -> IdempotentReplay | None:
        """Reserve the key.

        Returns ``None`` when the caller should proceed with the operation, or the
        stored response when this is a completed replay. Raises
        :class:`ConflictError` when an identical request is still in flight.
        """
        redis_key = self._key(scope, key, actor)
        reserved = await self._redis.set(
            redis_key,
            orjson.dumps({"state": "in_progress", "fingerprint": request_fingerprint}),
            nx=True,
            ex=self._ttl,
        )
        if reserved:
            return None

        raw = await self._redis.get(redis_key)
        if raw is None:
            # Expired between SET NX and GET — treat as a fresh request.
            return None

        record = orjson.loads(raw)
        if record.get("fingerprint") != request_fingerprint:
            raise ConflictError(
                "This idempotency key was already used with a different request body.",
                code="idempotency_key_reuse",
                details={"idempotency_key": key},
            )
        if record.get("state") == "in_progress":
            raise ConflictError(
                "A request with this idempotency key is still being processed.",
                code="idempotency_in_progress",
                details={"idempotency_key": key},
            )

        logger.info("idempotency.replayed", scope=scope, idempotency_key=key)
        return IdempotentReplay(
            status_code=record["status_code"],
            body=record["body"],
            headers=record.get("headers", {}),
        )

    async def complete(
        self,
        *,
        scope: str,
        key: str,
        actor: str,
        request_fingerprint: str,
        status_code: int,
        body: Any,
        headers: dict[str, str] | None = None,
    ) -> None:
        """Store the response so future replays short-circuit."""
        await self._redis.set(
            self._key(scope, key, actor),
            orjson.dumps(
                {
                    "state": "completed",
                    "fingerprint": request_fingerprint,
                    "status_code": status_code,
                    "body": body,
                    "headers": headers or {},
                }
            ),
            ex=self._ttl,
        )

    async def release(self, *, scope: str, key: str, actor: str) -> None:
        """Drop a reservation after a failure so the client can genuinely retry.

        Only call this for errors the client can fix or that are transient. A key
        held after a 500 would make the operation permanently unretryable.
        """
        await self._redis.delete(self._key(scope, key, actor))
