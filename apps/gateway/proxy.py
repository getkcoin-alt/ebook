"""The reverse proxy.

Two things here are easy to get wrong and expensive when you do:

**Hop-by-hop headers must be stripped in both directions.** They describe a single
connection, not the message. Forwarding ``Connection``, ``Transfer-Encoding`` or
``Upgrade`` to an upstream — or back to the client — produces framing bugs that look
like random truncation under load. RFC 9110 §7.6.1.

**Bodies stream.** Buffering a 40MB book download into gateway memory makes the
process's memory profile a function of the largest file any user requests, and
occupies a worker for the whole transfer.
"""

from __future__ import annotations

import time
from collections.abc import AsyncIterator
from dataclasses import dataclass

import httpx
from fastapi import Request
from fastapi.responses import Response, StreamingResponse
from routes import Route

from knowledgeos_core import ServiceUnavailableError, UpstreamError, get_logger
from knowledgeos_core.metrics import (
    upstream_request_duration_seconds,
    upstream_requests_total,
)
from knowledgeos_core.security import Principal
from settings import Settings

logger = get_logger(__name__)

#: Connection-scoped headers. Never forwarded in either direction.
HOP_BY_HOP = frozenset(
    {
        "connection",
        "keep-alive",
        "proxy-authenticate",
        "proxy-authorization",
        "proxy-connection",
        "te",
        "trailer",
        "trailers",
        "transfer-encoding",
        "upgrade",
    }
)

#: Additionally dropped from the inbound request. `host` must be the upstream's, and
#: `content-length` is recomputed by httpx from the body actually sent.
_DROP_FROM_REQUEST = HOP_BY_HOP | {"host", "content-length"}

#: Dropped from the upstream response. httpx has already decoded the body, so a
#: `content-encoding: gzip` header would describe bytes we are no longer sending,
#: and the length would be wrong.
_DROP_FROM_RESPONSE = HOP_BY_HOP | {"content-encoding", "content-length"}

#: Headers the gateway sets itself; an upstream must not be able to spoof them.
_UPSTREAM_MUST_NOT_SET = {"x-authenticated-user", "x-authenticated-roles"}


@dataclass(slots=True)
class ProxyResult:
    status_code: int
    headers: dict[str, str]
    body: bytes | None
    #: Set for streamed responses; the caller must consume it.
    stream: AsyncIterator[bytes] | None = None
    cacheable_body: bytes | None = None


class UpstreamPool:
    """One httpx client per upstream.

    Separate pools rather than one shared client: a saturated pool is how one slow
    service takes the gateway down for every other route. Isolating them means a
    stalled `ai` upstream cannot starve `books`.
    """

    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._clients: dict[str, httpx.AsyncClient] = {}

    def get(self, upstream: str) -> httpx.AsyncClient:
        if upstream not in self._clients:
            base_url = getattr(self._settings, f"{upstream}_service_url", None)
            if not base_url:
                raise ServiceUnavailableError(
                    f"No URL configured for the '{upstream}' service.",
                    details={"upstream": upstream},
                )
            self._clients[upstream] = httpx.AsyncClient(
                base_url=str(base_url).rstrip("/"),
                timeout=httpx.Timeout(
                    self._settings.upstream_read_timeout,
                    connect=self._settings.upstream_connect_timeout,
                    write=self._settings.upstream_write_timeout,
                    pool=self._settings.upstream_pool_timeout,
                ),
                limits=httpx.Limits(
                    max_connections=self._settings.upstream_max_connections,
                    max_keepalive_connections=self._settings.upstream_max_keepalive_connections,
                    keepalive_expiry=self._settings.upstream_keepalive_expiry,
                ),
                # A redirect from an internal service would be followed blindly, and
                # a compromised upstream could use it to make the gateway fetch an
                # arbitrary URL. Pass redirects back to the client instead.
                follow_redirects=False,
                headers={"User-Agent": "knowledgeos-gateway"},
            )
        return self._clients[upstream]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()


def build_forward_headers(
    request: Request,
    *,
    principal: Principal | None,
    request_id: str,
    internal_headers: dict[str, str],
) -> dict[str, str]:
    """Headers sent upstream."""
    # Keys are normalised to lower case throughout. HTTP header names are
    # case-insensitive, but a plain dict is not: a client sending `X-Request-ID`
    # arrives as `x-request-id`, and assigning `headers["X-Request-ID"]` would then
    # add a *second* entry rather than replacing it — httpx sends both and the
    # upstream sees `trace-me, trace-me`.
    headers = {
        key.lower(): value
        for key, value in request.headers.items()
        if key.lower() not in _DROP_FROM_REQUEST
        # A client must not be able to inject the identity headers the gateway
        # asserts — that would be a trivial privilege escalation.
        and key.lower() not in _UPSTREAM_MUST_NOT_SET
    }

    headers["x-request-id"] = request_id
    headers["x-forwarded-proto"] = request.url.scheme
    headers["x-forwarded-host"] = request.headers.get("host", "")

    # Append rather than replace, preserving the chain Railway's proxy started.
    client_host = request.client.host if request.client else ""
    existing = request.headers.get("x-forwarded-for")
    headers["x-forwarded-for"] = f"{existing}, {client_host}" if existing else client_host

    if principal is not None:
        # The token is already verified at the edge. These are a convenience for
        # upstreams; they are covered by the HMAC signature below, so an upstream
        # can trust them only because the signature proves the gateway sent them.
        headers["x-authenticated-user"] = principal.user_id
        headers["x-authenticated-roles"] = ",".join(principal.roles)

    headers.update({key.lower(): value for key, value in internal_headers.items()})
    return headers


def build_response_headers(upstream_response: httpx.Response, *, request_id: str) -> dict[str, str]:
    headers = {
        key.lower(): value
        for key, value in upstream_response.headers.items()
        if key.lower() not in _DROP_FROM_RESPONSE
    }
    headers["x-request-id"] = request_id
    return headers


class Proxy:
    def __init__(self, settings: Settings, pool: UpstreamPool) -> None:
        self._settings = settings
        self._pool = pool

    async def forward(
        self,
        request: Request,
        route: Route,
        *,
        principal: Principal | None,
        request_id: str,
        internal_headers: dict[str, str],
        body: bytes | None,
    ) -> ProxyResult:
        client = self._pool.get(route.upstream)
        headers = build_forward_headers(
            request, principal=principal, request_id=request_id, internal_headers=internal_headers
        )
        timeout = (
            httpx.Timeout(
                route.timeout,
                connect=self._settings.upstream_connect_timeout,
                write=self._settings.upstream_write_timeout,
                pool=self._settings.upstream_pool_timeout,
            )
            if route.timeout is not None
            else httpx.USE_CLIENT_DEFAULT
        )

        upstream_request = client.build_request(
            request.method,
            request.url.path,
            params=dict(request.query_params),
            content=body,
            headers=headers,
            timeout=timeout,
        )

        started = time.perf_counter()
        try:
            if route.stream:
                response = await client.send(upstream_request, stream=True)
            else:
                response = await client.send(upstream_request)
        except httpx.TimeoutException as exc:
            self._record(route.upstream, "timeout", started)
            logger.warning(
                "gateway.upstream_timeout", upstream=route.upstream, path=request.url.path
            )
            raise UpstreamError(
                f"The {route.upstream} service did not respond in time.",
                code="upstream_timeout",
                status_code=504,
                details={"upstream": route.upstream},
            ) from exc
        except httpx.HTTPError as exc:
            self._record(route.upstream, "error", started)
            logger.warning(
                "gateway.upstream_unreachable",
                upstream=route.upstream,
                path=request.url.path,
                error=str(exc),
            )
            raise ServiceUnavailableError(
                f"The {route.upstream} service is temporarily unavailable.",
                details={"upstream": route.upstream},
            ) from exc

        self._record(route.upstream, str(response.status_code), started)
        response_headers = build_response_headers(response, request_id=request_id)

        if route.stream:
            return ProxyResult(
                status_code=response.status_code,
                headers=response_headers,
                body=None,
                stream=_stream_and_close(response),
            )

        content = response.content
        # Only bodies small enough to be worth storing are offered to the cache.
        cacheable = content if len(content) <= self._settings.cache_max_body_bytes else None
        return ProxyResult(
            status_code=response.status_code,
            headers=response_headers,
            body=content,
            cacheable_body=cacheable,
        )

    def _record(self, upstream: str, status: str, started: float) -> None:
        service = self._settings.service_name
        upstream_requests_total.labels(service=service, upstream=upstream, status=status).inc()
        upstream_request_duration_seconds.labels(service=service, upstream=upstream).observe(
            time.perf_counter() - started
        )


async def _stream_and_close(response: httpx.Response) -> AsyncIterator[bytes]:
    """Relay an upstream body, always releasing the connection.

    Without the ``finally``, a client that disconnects mid-download leaks the
    upstream connection until the pool is exhausted.
    """
    try:
        async for chunk in response.aiter_raw():
            yield chunk
    finally:
        await response.aclose()


def to_response(result: ProxyResult) -> Response:
    if result.stream is not None:
        return StreamingResponse(
            result.stream, status_code=result.status_code, headers=result.headers
        )
    return Response(
        content=result.body,
        status_code=result.status_code,
        headers=result.headers,
        # Content-Type is already in the forwarded headers; setting it again here
        # would produce a duplicate.
        media_type=None,
    )
