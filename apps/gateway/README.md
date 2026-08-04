# API Gateway

**The only public ingress for the KnowledgeOS API.** Every other service sits on
Railway's private network and is unreachable from the internet.

- **Port** 8000 · **Owns no database** · **Depends on** Redis and the auth service's JWKS

## What it does, in request order

1. **Resolve** the path against the route table (`routes.py`), longest prefix wins.
2. **Authenticate** — verify the RS256 token *offline* against the cached JWKS. No
   call to the auth service on the request path.
3. **Check the ban denylist** in Redis, so a ban bites within seconds rather than
   waiting out the 15-minute token lifetime.
4. **Rate limit** by user id when authenticated, by client IP when not.
5. **Serve from cache** when the route allows it and the key matches.
6. **Proxy** to the upstream with hop-by-hop headers stripped and the identity
   asserted under an HMAC signature.
7. **Cache** the response when the route permits.

## The route table

`routes.py` is declarative on purpose. Adding a service is one `Route(...)` entry —
not a branch in a dispatch function, which is how a gateway ends up with subtly
different auth and caching rules per path.

Each route declares its upstream, whether anonymous callers are allowed, its rate
limit policy, its cache TTL and whether the cache varies per user, plus timeout and
streaming overrides.

```python
Route(
    "/v1/books",
    "books",
    public=True,
    cache_ttl=120,
    invalidate_on=("book.published", "book.updated"),
)
Route("/v1/library", "books", require_auth=True, cache_ttl=30, cache_vary_on_user=True)
Route("/v1/ai", "ai", require_auth=True, rate_limit="ai", timeout=120, stream=True)
```

## Things that are easy to get wrong, and how they are handled

**Cross-user cache leaks.** A key built from the path alone will store an
authenticated `/v1/library` response and hand it to the next anonymous caller. Every
key encodes an *auth scope*; a route must opt into `cache_vary_on_user` before a
per-user response is cached at all, and an authenticated request on a route that
does not vary by user is **not cached** rather than guessed about. There is an
explicit test proving no leak between two users and to anonymous.

**Hop-by-hop headers.** `Connection`, `Transfer-Encoding`, `Upgrade`, `TE`,
`Keep-Alive` and the `Proxy-*` family describe one connection, not the message.
Forwarding them produces framing bugs that look like random truncation under load
(RFC 9110 §7.6.1). They are stripped in both directions.

**Header case.** Header names are case-insensitive but a Python dict is not. A
client sending `X-Request-ID` arrives as `x-request-id`; assigning
`headers["X-Request-ID"]` would add a *second* entry and the upstream would receive
`trace-me, trace-me`. All keys are normalised to lower case.

**Identity spoofing.** `X-Authenticated-User` and `X-Authenticated-Roles` are
stripped from inbound requests before being set from the verified token — otherwise
any client could assert `superadmin`.

**Body buffering.** Downloads and AI streams set `stream=True`, so a 40MB book never
enters gateway memory. The relay always releases the upstream connection in a
`finally`, or a client disconnecting mid-download would leak it until pool exhaustion.

**Pool isolation.** One httpx client *per upstream*. A shared pool is how one slow
service starves every other route.

**Redirect following is off.** A compromised upstream could otherwise redirect the
gateway to an arbitrary URL. Redirects are passed back to the client.

## Failure behaviour

| Condition | Response |
|---|---|
| Upstream unreachable | `503 service_unavailable`, no traceback |
| Upstream timeout | `504 upstream_timeout` |
| Upstream 4xx/5xx | passed through unchanged |
| Redis down | rate limits and cache **fail open** — degraded, not broken |
| Denylist unreadable | fails open; window bounded by the token lifetime |
| One upstream down | only its routes fail; readiness reports it but stays `200` |

Upstream probes are registered as **optional** in readiness deliberately: one dead
service must not pull the entire gateway out of rotation when every other route
still works.

## Aggregated API documentation

`/docs` and `/openapi.json` merge every upstream's specification into one document,
cached in Redis. An upstream that is down or has docs disabled is simply absent
rather than failing the page.

The docs page is the only HTML the gateway serves, so it carries its own CSP
permitting the Swagger assets — the JSON API keeps core's `default-src 'none'`.

## Redis contract

| Key | Written by | Read by |
|---|---|---|
| `kos:gateway:cache:*` | gateway | gateway |
| `kos:gateway:openapi` | gateway | gateway |
| `kos:denylist:user:{user_id}` | **auth service** | gateway |
| `kos:ratelimit:{scope}:{id}` | core rate limiter | core rate limiter |

The denylist format is owned by the auth service — see
[`apps/auth/README.md`](../auth/README.md). Changing it is a breaking change.

Cache invalidation is coarse by design: an invalidating event (`book.published` and
friends) drops the whole response cache via `SCAN`. Tracking which keys contain a
given book id is more machinery than a brief dip in hit rate is worth, and a stale
catalogue page is the worse bug.

## Configuration

See [`.env.example`](.env.example). The essentials: `JWKS_URL`,
`INTERNAL_API_SECRET` (identical across all services), `REDIS_URL`, and one
`*_SERVICE_URL` per upstream — on Railway these are `<service>.railway.internal`.

## Running locally

```bash
cp .env.example .env
python -m main          # :8000, docs at /docs
```

The gateway starts even when no upstream is running; unreachable routes return 503
and readiness reports which ones are down.

## Tests

```bash
PYTHONPATH=apps/gateway pytest apps/gateway/tests -q
```

35 tests, no infrastructure required. Upstreams are `httpx.MockTransport` stubs and
Redis is `fakeredis` (with Lua, so the rate limiter's script genuinely executes).

**Note:** the suite sets `EVENTS_ENABLED=false` before importing `main`, because the
event consumer blocks on `XREADGROUP` and fakeredis does not implement the blocking
form. Cache invalidation is covered directly against `ResponseCache`.

## Operational notes

- **Scale this first.** It fronts everything; p95 latency here is the platform's p95.
- **Health**: `/health` is liveness. Railway's healthcheck points there, not at
  `/health/ready` — readiness reflects upstream state, and restarting the gateway
  cannot fix a dead books service.
- `X-Cache: HIT|MISS|BYPASS` on every response makes cache behaviour visible in the
  browser's network tab.
- Watch `knowledgeos_upstream_requests_total` and
  `knowledgeos_circuit_breaker_state` to see which dependency is degrading.
