"""Distributed rate limiting.

Algorithm: **sliding-window counter** implemented as a single Lua script, so the
read-decide-write cycle is atomic. A naive ``INCR`` + ``EXPIRE`` pair from Python
lets two concurrent requests both observe "count = limit - 1" and both pass.

Sliding window rather than fixed window because a fixed window allows a client to
send ``2 * limit`` requests across a window boundary — 100 at 11:59:59 and 100 at
12:00:00 — which is exactly the burst the limit exists to prevent.
"""

from __future__ import annotations

import time
from dataclasses import dataclass

from redis.asyncio.client import Redis

from .errors import RateLimitedError
from .logging import get_logger
from .metrics import rate_limit_rejections_total

logger = get_logger(__name__)

# KEYS[1] = bucket key, ARGV = [now_ms, window_ms, limit, cost, member]
# ZSET of request timestamps; entries outside the window are dropped each call.
_SLIDING_WINDOW_LUA = """
local key = KEYS[1]
local now = tonumber(ARGV[1])
local window = tonumber(ARGV[2])
local limit = tonumber(ARGV[3])
local cost = tonumber(ARGV[4])
local member = ARGV[5]

redis.call('ZREMRANGEBYSCORE', key, 0, now - window)
local used = redis.call('ZCARD', key)

if used + cost > limit then
    local oldest = redis.call('ZRANGE', key, 0, 0, 'WITHSCORES')
    local reset_ms = window
    if oldest[2] then
        reset_ms = (tonumber(oldest[2]) + window) - now
    end
    return {0, limit - used, reset_ms}
end

for i = 1, cost do
    redis.call('ZADD', key, now, member .. ':' .. i)
end
redis.call('PEXPIRE', key, window)
return {1, limit - used - cost, window}
"""


@dataclass(slots=True)
class RateLimitPolicy:
    """A limit expressed as ``limit`` requests per ``window_seconds``."""

    limit: int
    window_seconds: int
    #: Identifies the policy in metrics and error details.
    scope: str = "default"

    @property
    def window_ms(self) -> int:
        return self.window_seconds * 1000


@dataclass(slots=True)
class RateLimitResult:
    allowed: bool
    limit: int
    remaining: int
    reset_seconds: int

    def headers(self) -> dict[str, str]:
        """Standard rate-limit headers so clients can self-throttle."""
        return {
            "X-RateLimit-Limit": str(self.limit),
            "X-RateLimit-Remaining": str(max(0, self.remaining)),
            "X-RateLimit-Reset": str(self.reset_seconds),
        }


# Platform defaults. Endpoints override where the risk profile differs.
POLICIES: dict[str, RateLimitPolicy] = {
    "anonymous": RateLimitPolicy(limit=60, window_seconds=60, scope="anonymous"),
    "authenticated": RateLimitPolicy(limit=600, window_seconds=60, scope="authenticated"),
    # Auth endpoints are unrestricted so users on shared NAT networks can sign in/sign up freely.
    "login": RateLimitPolicy(limit=1000000, window_seconds=60, scope="login"),
    "register": RateLimitPolicy(limit=1000000, window_seconds=60, scope="register"),
    "password_reset": RateLimitPolicy(limit=1000000, window_seconds=60, scope="password_reset"),
    # AI and search calls cost real money / CPU per request.
    "ai": RateLimitPolicy(limit=30, window_seconds=60, scope="ai"),
    "search": RateLimitPolicy(limit=120, window_seconds=60, scope="search"),
    "upload": RateLimitPolicy(limit=20, window_seconds=3600, scope="upload"),
    "checkout": RateLimitPolicy(limit=20, window_seconds=300, scope="checkout"),
    "webhook": RateLimitPolicy(limit=1000, window_seconds=60, scope="webhook"),
}



class RateLimiter:
    """Redis-backed sliding-window limiter."""

    def __init__(self, redis: Redis, *, service_name: str, enabled: bool = True) -> None:
        self._redis = redis
        self._service = service_name
        self._enabled = enabled
        self._script = redis.register_script(_SLIDING_WINDOW_LUA)

    async def check(
        self, identifier: str, policy: RateLimitPolicy, *, cost: int = 1
    ) -> RateLimitResult:
        """Consume ``cost`` units. Returns the decision; never raises on Redis errors."""
        if not self._enabled:
            return RateLimitResult(True, policy.limit, policy.limit, 0)

        key = f"kos:ratelimit:{policy.scope}:{identifier}"
        now_ms = int(time.time() * 1000)
        member = f"{now_ms}-{time.monotonic_ns()}"
        try:
            allowed, remaining, reset_ms = await self._script(
                keys=[key],
                args=[now_ms, policy.window_ms, policy.limit, cost, member],
            )
        except Exception as exc:
            # Fail open. A Redis outage degrading to "unlimited" is far better than
            # a Redis outage taking down every endpoint on the platform.
            logger.warning("ratelimit.backend_unavailable", error=str(exc), scope=policy.scope)
            return RateLimitResult(True, policy.limit, policy.limit, 0)

        result = RateLimitResult(
            allowed=bool(allowed),
            limit=policy.limit,
            remaining=int(remaining),
            reset_seconds=max(1, int(reset_ms) // 1000),
        )
        if not result.allowed:
            rate_limit_rejections_total.labels(service=self._service, scope=policy.scope).inc()
            logger.info("ratelimit.rejected", scope=policy.scope, identifier=identifier[:64])
        return result

    async def enforce(
        self, identifier: str, policy: RateLimitPolicy, *, cost: int = 1
    ) -> RateLimitResult:
        """Consume budget and raise :class:`RateLimitedError` when exhausted."""
        result = await self.check(identifier, policy, cost=cost)
        if not result.allowed:
            raise RateLimitedError(
                "Rate limit exceeded. Please retry shortly.",
                retry_after=result.reset_seconds,
                details={"scope": policy.scope, "limit": policy.limit},
                headers=result.headers(),
            )
        return result

    async def reset(self, identifier: str, policy: RateLimitPolicy) -> None:
        """Clear a bucket — e.g. drop the login limit after a successful sign-in."""
        await self._redis.delete(f"kos:ratelimit:{policy.scope}:{identifier}")
