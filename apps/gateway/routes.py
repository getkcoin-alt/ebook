"""The route table.

Declarative on purpose. Adding a service is one entry here, not a branch in a
dispatch function — a wall of ``if path.startswith(...)`` is how a gateway acquires
subtly inconsistent auth and caching rules per route.

Matching is **longest-prefix-wins**, so ``/v1/books/{id}/download`` can carry
different rules from ``/v1/books`` without ordering games.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from functools import lru_cache


@dataclass(frozen=True, slots=True)
class Route:
    """How one path prefix is handled."""

    prefix: str
    #: Key in ServiceSettings: ``<upstream>_service_url``.
    upstream: str

    #: Anonymous callers allowed. False means a valid token is required at the edge.
    public: bool = False
    #: Reject the request here when the token is missing/invalid, rather than
    #: forwarding it. Public routes still decode a token when one is present, so
    #: upstreams can vary the response for a signed-in user.
    require_auth: bool = False

    #: Named policy from knowledgeos_core.ratelimit.POLICIES.
    rate_limit: str = "authenticated"
    #: Policy applied when the caller is anonymous.
    anonymous_rate_limit: str = "anonymous"

    #: Cache successful GETs for this many seconds. 0 disables caching.
    cache_ttl: int = 0
    #: Never cache a response for a signed-in user under an anonymous key, or vice
    #: versa. When True the cache key includes the user id, so per-user responses
    #: stay per-user.
    cache_vary_on_user: bool = False

    #: Read timeout override. AI and automation are legitimately slow.
    timeout: float | None = None
    #: Do not buffer the body — downloads and SSE must stream.
    stream: bool = False

    #: Events that invalidate this route's cached responses.
    invalidate_on: tuple[str, ...] = field(default_factory=tuple)

    @property
    def depth(self) -> int:
        return self.prefix.count("/")


# Ordered by readability, not precedence — resolution sorts by prefix length.
ROUTES: tuple[Route, ...] = (
    # ---- auth ------------------------------------------------------------
    # Credential endpoints get their own tight budgets. These are the brute-force
    # targets on the platform, and they are never cached.
    Route("/v1/auth/login", "auth", public=True, rate_limit="login", anonymous_rate_limit="login"),
    Route(
        "/v1/auth/register",
        "auth",
        public=True,
        rate_limit="register",
        anonymous_rate_limit="register",
    ),
    Route(
        "/v1/auth/forgot-password",
        "auth",
        public=True,
        rate_limit="password_reset",
        anonymous_rate_limit="password_reset",
    ),
    Route(
        "/v1/auth/reset-password",
        "auth",
        public=True,
        rate_limit="password_reset",
        anonymous_rate_limit="password_reset",
    ),
    Route(
        "/v1/auth/refresh", "auth", public=True, rate_limit="login", anonymous_rate_limit="login"
    ),
    Route(
        "/v1/auth/verify-email",
        "auth",
        public=True,
        rate_limit="password_reset",
        anonymous_rate_limit="password_reset",
    ),
    Route("/v1/auth/oauth", "auth", public=True, rate_limit="login", anonymous_rate_limit="login"),
    # Everything else under /v1/auth needs a token (profile, sessions, MFA).
    Route("/v1/auth", "auth", public=False, require_auth=True),
    Route("/.well-known/jwks.json", "auth", public=True, rate_limit="anonymous", cache_ttl=300),
    # ---- catalogue -------------------------------------------------------
    # The read-heavy public surface. Cached, and invalidated by book events.
    Route(
        "/v1/books",
        "books",
        public=True,
        cache_ttl=120,
        invalidate_on=("book.published", "book.updated", "book.unpublished", "book.deleted"),
    ),
    # Downloads are per-user, entitlement-checked and large: never cached, always
    # streamed, and given a longer timeout for the redirect round trip.
    Route("/v1/books/downloads", "books", require_auth=True, cache_ttl=0, stream=True, timeout=60),
    Route(
        "/v1/authors",
        "books",
        public=True,
        cache_ttl=300,
        invalidate_on=("book.published", "book.updated"),
    ),
    Route(
        "/v1/categories",
        "books",
        public=True,
        cache_ttl=600,
        invalidate_on=("book.published", "book.updated", "book.deleted"),
    ),
    Route(
        "/v1/reviews",
        "books",
        public=True,
        cache_ttl=60,
        invalidate_on=("review.created", "review.deleted"),
    ),
    # Personal shelves vary per user; the cache key must include the user id.
    Route("/v1/library", "books", require_auth=True, cache_ttl=30, cache_vary_on_user=True),
    Route("/v1/wishlist", "books", require_auth=True),
    Route("/v1/bookmarks", "books", require_auth=True),
    Route("/v1/reading-progress", "books", require_auth=True),
    # ---- search ----------------------------------------------------------
    # Deliberately **not** cached. Caching search would break the two things the
    # search service observes: a cached hit never reaches it, so a popular query
    # would be counted once per TTL instead of once per search — which undercounts
    # exactly the queries the zero-result report is for, and inverts the trending
    # ranking so that *less* popular queries rank higher. Meilisearch answers in
    # single-digit milliseconds; there is nothing here worth buying with that.
    Route(
        "/v1/search",
        "search",
        public=True,
        rate_limit="search",
        anonymous_rate_limit="search",
        cache_ttl=0,
    ),
    # Trending is a whole-platform aggregate that nobody observes, so it caches
    # freely — and it is read on every page load.
    Route("/v1/search/trending", "search", public=True, rate_limit="anonymous", cache_ttl=60),
    # ---- ai --------------------------------------------------------------
    # Slow by nature and costs real money per call, so a tight limit and a long
    # timeout. Streaming for chat.
    Route("/v1/ai", "ai", require_auth=True, rate_limit="ai", timeout=120, stream=True),
    # ---- commerce --------------------------------------------------------
    Route("/v1/orders", "payment", require_auth=True, rate_limit="checkout"),
    Route("/v1/payments", "payment", require_auth=True, rate_limit="checkout"),
    # Which gateways this deployment can use. Public because the checkout page
    # reads it before the customer has signed in, and it returns only public keys.
    Route("/v1/payments/providers", "payment", public=True, cache_ttl=300),
    # Quoting a cart and checking a coupon are read-only and are called from the
    # checkout page before sign-in. Never cached: the answer depends on the cart in
    # the request body, and on who is asking.
    Route("/v1/checkout", "payment", public=True, rate_limit="anonymous", cache_ttl=0),
    Route("/v1/coupons", "payment", public=True, rate_limit="anonymous", cache_ttl=0),
    # Webhooks arrive from Razorpay/Stripe with no user token; their signature is
    # what authenticates them. Generous limit: providers retry in bursts.
    #
    # Never cached and never rewritten — the payment service verifies the signature
    # over the *raw* body, so anything that alters those bytes in transit breaks
    # verification and every callback starts failing closed.
    Route(
        "/v1/webhooks",
        "payment",
        public=True,
        rate_limit="webhook",
        anonymous_rate_limit="webhook",
        cache_ttl=0,
        timeout=30,
    ),
    Route("/v1/subscriptions", "payment", require_auth=True),
    # Plans are a public price list, and they change rarely.
    Route("/v1/plans", "payment", public=True, cache_ttl=300),
    Route("/v1/invoices", "payment", require_auth=True),
    Route("/v1/affiliate", "payment", require_auth=True),
    # ---- everything else -------------------------------------------------
    Route("/v1/notifications", "notification", require_auth=True),
    Route("/v1/automation", "automation", require_auth=True, timeout=60),
    # `/v1/admin` is a shared prefix: the admin service owns it in general, but
    # each service serves the admin surface for the data it owns. Longest-prefix
    # matching sends those to the right place; without these lines they would all
    # be proxied to `admin`, which has never heard of a coupon.
    Route("/v1/admin", "admin", require_auth=True, timeout=60),
    Route("/v1/admin/books", "books", require_auth=True, timeout=60),
    Route("/v1/admin/orders", "payment", require_auth=True, timeout=60),
    Route("/v1/admin/coupons", "payment", require_auth=True),
    Route("/v1/admin/plans", "payment", require_auth=True),
    Route("/v1/admin/invoices", "payment", require_auth=True),
    Route("/v1/admin/webhooks", "payment", require_auth=True),
    Route("/v1/admin/revenue", "payment", require_auth=True, timeout=60),
    # A reindex can run for minutes, so it gets a much longer budget than the
    # default admin route.
    Route("/v1/admin/search", "search", require_auth=True, timeout=300),
)


@lru_cache(maxsize=4096)
def resolve(path: str) -> Route | None:
    """Longest matching prefix for a request path, or ``None``.

    Cached because the table is static and this runs on every request. The cache is
    bounded, so a crawler hitting unique paths cannot grow it without limit.
    """
    best: Route | None = None
    for route in ROUTES:
        matches = path == route.prefix or path.startswith(route.prefix + "/")
        if matches and (best is None or len(route.prefix) > len(best.prefix)):
            best = route
    return best


def upstream_names() -> tuple[str, ...]:
    """Distinct upstreams referenced by the table, for health checks and metrics."""
    return tuple(sorted({route.upstream for route in ROUTES}))


#: Every event any route listens to, so the consumer subscribes once.
def invalidation_events() -> tuple[str, ...]:
    events: set[str] = set()
    for route in ROUTES:
        events.update(route.invalidate_on)
    return tuple(sorted(events))
