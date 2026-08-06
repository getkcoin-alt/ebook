"""API gateway — the only public ingress for the KnowledgeOS API.

Per request: resolve the route, enforce the rate limit, verify the token, check the
ban denylist, try the cache, proxy upstream, cache the result.

Everything else on the platform sits on Railway's private network and is not
reachable from the internet.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

from cache import ResponseCache, auth_scope, build_key
from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, Response
from proxy import Proxy, UpstreamPool, set_response_header, to_response
from routes import invalidation_events, resolve, upstream_names

from knowledgeos_core import (
    Components,
    NotFoundError,
    UnauthorizedError,
    create_app,
    get_logger,
    run,
)
from knowledgeos_core.app import AppContext
from knowledgeos_core.deps import Ctx
from knowledgeos_core.ratelimit import POLICIES, RateLimitPolicy
from knowledgeos_core.security import Principal, TokenVerifier, hash_body, sign_internal_request
from settings import settings

logger = get_logger(__name__)

router = APIRouter()

#: Methods that may be served from, or written to, the cache.
_CACHEABLE_METHODS = frozenset({"GET", "HEAD"})


# ---------------------------------------------------------------------------
# Lifecycle
# ---------------------------------------------------------------------------


async def _bootstrap(ctx: AppContext) -> None:
    pool = UpstreamPool(settings)
    ctx.extras["pool"] = pool
    ctx.extras["proxy"] = Proxy(settings, pool)
    ctx.extras["verifier"] = TokenVerifier(
        jwks_url=settings.jwks_url or "",
        issuer=settings.jwt_issuer,
        audience=settings.jwt_audience,
        algorithms=(settings.jwt_algorithm,),
        cache_ttl=settings.jwks_cache_ttl,
    )
    if ctx.redis is not None:
        ctx.extras["cache"] = ResponseCache(ctx.redis.client, settings)

    # Upstream reachability is reported by readiness but never blocks it: one dead
    # service must not pull the whole gateway out of rotation, because every other
    # route still works.
    for name in upstream_names():
        ctx.health.register(f"upstream:{name}", _upstream_probe(ctx, name), required=False)

    if ctx.consumer is not None and ctx.extras.get("cache") is not None:
        cache: ResponseCache = ctx.extras["cache"]

        async def _invalidate(event: Any) -> None:
            await cache.invalidate_all()

        for event_type in invalidation_events():
            ctx.consumer.on(event_type)(_invalidate)
        logger.info("gateway.cache_invalidation_wired", events=list(invalidation_events()))

    logger.info("gateway.ready", upstreams=list(upstream_names()))


def _upstream_probe(ctx: AppContext, name: str):  # type: ignore[no-untyped-def]
    """Readiness probe for one upstream, with a short shared cache.

    ``/health/ready`` is polled every few seconds; without the snapshot each poll
    would fan out to nine services and the probes would become the load.
    """
    state: dict[str, Any] = {"at": 0.0, "result": {"status": "unknown"}}

    async def probe() -> dict[str, Any]:
        now = time.monotonic()
        if now - state["at"] < settings.upstream_health_cache_seconds:
            return state["result"]  # type: ignore[no-any-return]
        pool: UpstreamPool = ctx.extras["pool"]
        try:
            client = pool.get(name)
            response = await client.get("/health", timeout=settings.upstream_health_timeout)
            result = {"status": "up" if response.status_code == 200 else "down"}
        except Exception as exc:
            result = {"status": "down", "error": type(exc).__name__}
        state["at"], state["result"] = now, result
        return result

    return probe


async def _shutdown(ctx: AppContext) -> None:
    pool: UpstreamPool | None = ctx.extras.get("pool")
    if pool is not None:
        await pool.aclose()


# ---------------------------------------------------------------------------
# Request pipeline
# ---------------------------------------------------------------------------


async def _authenticate(ctx: AppContext, request: Request) -> Principal | None:
    """Verify the bearer token if present. Returns ``None`` for anonymous callers."""
    header = request.headers.get("authorization", "")
    if not header.lower().startswith("bearer "):
        return None
    token = header[7:].strip()
    if not token:
        return None
    verifier: TokenVerifier = ctx.extras["verifier"]
    return await verifier.verify(token)


async def _assert_not_banned(ctx: AppContext, principal: Principal) -> None:
    """Reject a banned user.

    Access tokens stay cryptographically valid until they expire, so a ban would
    otherwise take up to 15 minutes to bite. The auth service writes this key; the
    format is part of the platform contract (see apps/auth/README.md).
    """
    if not settings.denylist_enabled or ctx.redis is None:
        return
    key = settings.denylist_key_template.format(user_id=principal.user_id)
    try:
        banned = await ctx.redis.client.exists(key)
    except Exception as exc:
        # Fail open. A Redis outage must not lock every user out of the platform;
        # the window is bounded by the access-token lifetime.
        logger.warning("gateway.denylist_unavailable", error=str(exc))
        return
    if banned:
        logger.info("gateway.banned_user_blocked", user_id=principal.user_id)
        raise UnauthorizedError("This account has been suspended.", code="account_banned")


def _policy_for(route: Any, principal: Principal | None) -> RateLimitPolicy:
    name = route.rate_limit if principal is not None else route.anonymous_rate_limit
    return POLICIES.get(name, POLICIES["anonymous"])


def _identifier(request: Request, principal: Principal | None) -> str:
    if principal is not None:
        return f"user:{principal.user_id}"
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        # Only the first entry — later ones are client-controlled.
        return f"ip:{forwarded.split(',')[0].strip()}"
    return f"ip:{request.client.host if request.client else 'unknown'}"


@router.api_route(
    "/{full_path:path}",
    methods=["GET", "POST", "PUT", "PATCH", "DELETE", "HEAD", "OPTIONS"],
    include_in_schema=False,
)
async def gateway(request: Request, full_path: str, ctx: Ctx) -> Response:
    path = request.url.path
    route = resolve(path)
    if route is None:
        raise NotFoundError("No route matches this path.", details={"path": path})

    principal = await _authenticate(ctx, request)
    if route.require_auth and principal is None:
        raise UnauthorizedError("An access token is required for this endpoint.")
    if principal is not None:
        await _assert_not_banned(ctx, principal)

    # --- rate limit -------------------------------------------------------
    rate_headers: dict[str, str] = {}
    if settings.rate_limit_enabled and ctx.limiter is not None:
        result = await ctx.limiter.enforce(
            _identifier(request, principal), _policy_for(route, principal)
        )
        rate_headers = result.headers()

    request_id: str = getattr(request.state, "request_id", "")
    cache: ResponseCache | None = ctx.extras.get("cache")

    # --- cache lookup -----------------------------------------------------
    cache_key: str | None = None
    if (
        cache is not None
        and route.cache_ttl > 0
        and request.method in _CACHEABLE_METHODS
        and "no-cache" not in request.headers.get("cache-control", "").lower()
    ):
        scope = auth_scope(route, principal)
        if scope is not None:
            cache_key = build_key(
                method=request.method,
                path=path,
                query=str(request.url.query),
                scope=scope,
            )
            hit = await cache.get(cache_key)
            if hit is not None:
                return Response(
                    content=hit.body,
                    status_code=hit.status_code,
                    headers={
                        **hit.headers,
                        **{k.lower(): v for k, v in rate_headers.items()},
                        "x-cache": "HIT",
                        "x-request-id": request_id,
                    },
                )

    # --- proxy ------------------------------------------------------------
    body = await request.body() if request.method not in {"GET", "HEAD"} else None
    proxy: Proxy = ctx.extras["proxy"]

    timestamp, signature = sign_internal_request(
        settings.internal_api_secret,
        method=request.method,
        path=path,
        body_hash=hash_body(body or b""),
    )
    result = await proxy.forward(
        request,
        route,
        principal=principal,
        request_id=request_id,
        internal_headers={
            "X-Internal-Timestamp": timestamp,
            "X-Internal-Signature": signature,
            "X-Internal-Service": settings.service_name,
        },
        body=body,
    )

    for name, value in rate_headers.items():
        set_response_header(result.headers, name, value)
    set_response_header(result.headers, "x-cache", "MISS" if cache_key else "BYPASS")

    if cache_key is not None and result.cacheable_body is not None:
        await cache.store(  # type: ignore[union-attr]
            cache_key,
            status_code=result.status_code,
            # A mapping is fine here: the cache refuses any response carrying a
            # `Set-Cookie`, which is the only header the proxy keeps repeated.
            headers=dict(result.headers),
            body=result.cacheable_body,
            ttl=route.cache_ttl,
        )

    return to_response(result)


# ---------------------------------------------------------------------------
# Aggregated API documentation
# ---------------------------------------------------------------------------

docs_router = APIRouter(tags=["docs"], include_in_schema=False)


@docs_router.get("/openapi.json")
async def aggregated_openapi(ctx: Ctx) -> JSONResponse:
    """Merge every upstream's OpenAPI document into one spec.

    Degrades gracefully: an upstream that is down or has docs disabled is simply
    absent from the merged document rather than failing the whole page.
    """
    if not settings.openapi_enabled:
        raise NotFoundError("API documentation is disabled.")

    cache_key = "kos:gateway:openapi"
    if ctx.redis is not None:
        cached = await ctx.redis.get_json(cache_key)
        if cached is not None:
            return JSONResponse(cached)

    pool: UpstreamPool = ctx.extras["pool"]
    merged: dict[str, Any] = {
        "openapi": "3.1.0",
        "info": {
            "title": "KnowledgeOS API",
            "version": settings.service_version,
            "description": "Aggregated specification for every KnowledgeOS service.",
        },
        "paths": {},
        "components": {
            "schemas": {},
            "securitySchemes": {
                "bearerAuth": {"type": "http", "scheme": "bearer", "bearerFormat": "JWT"}
            },
        },
        "tags": [],
    }

    async def fetch(name: str) -> tuple[str, dict[str, Any] | None]:
        try:
            client = pool.get(name)
            response = await client.get("/openapi.json", timeout=settings.openapi_fetch_timeout)
            if response.status_code == 200:
                return name, response.json()
        except Exception as exc:
            logger.info("gateway.openapi_fetch_failed", upstream=name, error=type(exc).__name__)
        return name, None

    for _name, spec in await asyncio.gather(*(fetch(n) for n in upstream_names())):
        if not spec:
            continue
        merged["paths"].update(spec.get("paths", {}))
        merged["components"]["schemas"].update(spec.get("components", {}).get("schemas", {}))
        for tag in spec.get("tags", []):
            if tag not in merged["tags"]:
                merged["tags"].append(tag)

    if ctx.redis is not None:
        await ctx.redis.set_json(cache_key, merged, ttl=settings.openapi_cache_ttl)
    return JSONResponse(merged)


@docs_router.get("/docs")
async def docs_page() -> HTMLResponse:
    """Swagger UI for the aggregated specification."""
    if not settings.openapi_enabled:
        raise NotFoundError("API documentation is disabled.")
    html = f"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>KnowledgeOS API</title>
<link rel="stylesheet" href="{settings.swagger_ui_css_url}">
</head>
<body>
<div id="swagger-ui"></div>
<script src="{settings.swagger_ui_js_url}" crossorigin></script>
<script>
window.onload = () => SwaggerUIBundle({{
  url: '/openapi.json',
  dom_id: '#swagger-ui',
  deepLinking: true,
  persistAuthorization: true,
}});
</script>
</body>
</html>"""
    # This is the one HTML page the gateway serves, so it needs a CSP that permits
    # the Swagger assets — core's default-src 'none' would block them.
    return HTMLResponse(html, headers={"Content-Security-Policy": settings.docs_csp})


# ---------------------------------------------------------------------------
# Application
# ---------------------------------------------------------------------------

app = create_app(
    settings=settings,
    components=Components(
        redis=True,
        # Tokens are verified here against the auth service's JWKS. The gateway
        # itself declares auth=False because it uses its own verifier instance
        # rather than core's request dependency.
        auth=False,
        event_consumer=settings.event_consumer_group if settings.events_enabled else None,
    ),
    # docs_router first: its concrete paths must win over the catch-all.
    routers=[docs_router, router],
    on_startup=[_bootstrap],
    on_shutdown=[_shutdown],
    description="Public API gateway: routing, rate limiting, edge auth and caching.",
)


if __name__ == "__main__":
    run("main:app", settings)
