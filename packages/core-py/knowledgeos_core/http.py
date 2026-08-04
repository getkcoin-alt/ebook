"""Client for calling other KnowledgeOS services.

Three things every internal call gets for free:

* **Correlation propagation** — the request id travels with the call, so one grep
  reconstructs a request that crossed four services.
* **HMAC signing** — internal endpoints verify the signature, so private-network
  reachability alone is not authorisation.
* **A circuit breaker** — when a dependency is down, we stop hammering it and fail
  fast. Without this, one slow service exhausts every caller's connection pool and a
  single failure becomes a platform outage.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

import httpx
import orjson
from tenacity import (
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential_jitter,
)

from .errors import ServiceUnavailableError, UpstreamError
from .logging import get_logger, request_id_ctx, trace_id_ctx
from .metrics import (
    circuit_breaker_state,
    upstream_request_duration_seconds,
    upstream_requests_total,
)
from .security import hash_body, sign_internal_request

logger = get_logger(__name__)


class BreakerState(StrEnum):
    CLOSED = "closed"  # normal operation
    OPEN = "open"  # failing fast
    HALF_OPEN = "half_open"  # probing for recovery


@dataclass
class CircuitBreaker:
    """Per-upstream breaker.

    Opens after ``failure_threshold`` consecutive failures, stays open for
    ``recovery_timeout`` seconds, then allows a single probe. A successful probe
    closes it; a failed probe re-opens it for another full timeout.
    """

    name: str
    service: str
    failure_threshold: int = 5
    recovery_timeout: float = 30.0
    _state: BreakerState = field(default=BreakerState.CLOSED, init=False)
    _failures: int = field(default=0, init=False)
    _opened_at: float = field(default=0.0, init=False)

    def _set_state(self, state: BreakerState) -> None:
        self._state = state
        circuit_breaker_state.labels(service=self.service, upstream=self.name).set(
            {BreakerState.CLOSED: 0, BreakerState.HALF_OPEN: 1, BreakerState.OPEN: 2}[state]
        )

    def before_request(self) -> None:
        if self._state is BreakerState.OPEN:
            if (time.monotonic() - self._opened_at) >= self.recovery_timeout:
                self._set_state(BreakerState.HALF_OPEN)
                logger.info("circuit.half_open", upstream=self.name)
            else:
                raise ServiceUnavailableError(
                    f"The {self.name} service is temporarily unavailable.",
                    details={"upstream": self.name, "circuit": "open"},
                )

    def on_success(self) -> None:
        if self._state is not BreakerState.CLOSED:
            logger.info("circuit.closed", upstream=self.name)
        self._failures = 0
        self._set_state(BreakerState.CLOSED)

    def on_failure(self) -> None:
        self._failures += 1
        if self._state is BreakerState.HALF_OPEN or self._failures >= self.failure_threshold:
            self._opened_at = time.monotonic()
            self._set_state(BreakerState.OPEN)
            logger.warning(
                "circuit.opened", upstream=self.name, consecutive_failures=self._failures
            )


class ServiceClient:
    """HTTP client for one downstream service."""

    def __init__(
        self,
        base_url: str,
        *,
        name: str,
        service_name: str,
        internal_secret: str,
        timeout: float = 15.0,
        max_connections: int = 100,
    ) -> None:
        self._name = name
        self._service = service_name
        self._secret = internal_secret
        self._client = httpx.AsyncClient(
            base_url=base_url.rstrip("/"),
            timeout=httpx.Timeout(timeout, connect=5.0),
            limits=httpx.Limits(
                max_connections=max_connections,
                max_keepalive_connections=20,
                keepalive_expiry=30.0,
            ),
            follow_redirects=False,  # an internal call should never be redirected
            headers={"User-Agent": f"knowledgeos-{service_name}/internal"},
        )
        self._breaker = CircuitBreaker(name=name, service=service_name)

    def _headers(
        self, method: str, path: str, body: bytes, extra: dict[str, str] | None
    ) -> dict[str, str]:
        digest = hash_body(body)
        timestamp, signature = sign_internal_request(
            self._secret, method=method, path=path, body_hash=digest
        )
        headers = {
            "X-Internal-Timestamp": timestamp,
            "X-Internal-Signature": signature,
            "X-Internal-Service": self._service,
        }
        if digest:
            headers["X-Internal-Body-Hash"] = digest
        if (rid := request_id_ctx.get()) is not None:
            headers["X-Request-ID"] = rid
        if (tid := trace_id_ctx.get()) is not None:
            headers["X-Trace-ID"] = tid
        if extra:
            headers.update(extra)
        return headers

    @retry(
        # Only connection/timeout errors are retried. A 4xx is deterministic and a
        # 5xx may well have had a side effect, so blind retries risk duplicates.
        retry=retry_if_exception_type((httpx.ConnectError, httpx.ReadTimeout, httpx.PoolTimeout)),
        stop=stop_after_attempt(3),
        wait=wait_exponential_jitter(initial=0.2, max=2.0),
        reraise=True,
    )
    async def _send(self, request: httpx.Request) -> httpx.Response:
        return await self._client.send(request)

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        headers: dict[str, str] | None = None,
        content: bytes | None = None,
    ) -> httpx.Response:
        self._breaker.before_request()

        # Serialise ourselves so the signed body hash is byte-identical to what is
        # actually transmitted; letting httpx encode separately would risk a mismatch.
        if content is not None:
            body = content
        elif json is not None:
            body = orjson.dumps(json)
        else:
            body = b""

        request = self._client.build_request(
            method,
            path,
            params=params,
            content=body or None,
            headers={
                **self._headers(method, path, body, headers),
                **({"Content-Type": "application/json"} if json is not None else {}),
            },
        )

        started = time.perf_counter()
        try:
            response = await self._send(request)
        except httpx.HTTPError as exc:
            self._breaker.on_failure()
            upstream_requests_total.labels(
                service=self._service, upstream=self._name, status="error"
            ).inc()
            logger.warning(
                "upstream.request_failed", upstream=self._name, path=path, error=str(exc)
            )
            raise UpstreamError(
                f"Could not reach the {self._name} service.",
                details={"upstream": self._name},
            ) from exc
        finally:
            upstream_request_duration_seconds.labels(
                service=self._service, upstream=self._name
            ).observe(time.perf_counter() - started)

        upstream_requests_total.labels(
            service=self._service, upstream=self._name, status=str(response.status_code)
        ).inc()

        # Only the upstream's own failures should trip the breaker. A 404 or 422 is
        # a healthy service telling us something true about the request.
        if response.status_code >= 500:
            self._breaker.on_failure()
        else:
            self._breaker.on_success()
        return response

    async def get_json(self, path: str, **kwargs: Any) -> Any:
        response = await self.request("GET", path, **kwargs)
        return self._unwrap(response)

    async def post_json(self, path: str, json: Any = None, **kwargs: Any) -> Any:
        response = await self.request("POST", path, json=json, **kwargs)
        return self._unwrap(response)

    def _unwrap(self, response: httpx.Response) -> Any:
        if response.is_success:
            return response.json() if response.content else None
        try:
            payload = response.json()
            error = payload.get("error", {})
            message = error.get("message", "Upstream request failed.")
            code = error.get("code", "upstream_error")
        except Exception:
            message, code = "Upstream request failed.", "upstream_error"
        raise UpstreamError(
            message,
            code=code,
            status_code=response.status_code if response.status_code < 500 else 502,
            details={"upstream": self._name},
        )

    async def aclose(self) -> None:
        await self._client.aclose()


class ServiceRegistry:
    """Lazily-created, reused clients keyed by service name."""

    def __init__(self, settings: Any) -> None:
        self._settings = settings
        self._clients: dict[str, ServiceClient] = {}

    def get(self, name: str) -> ServiceClient:
        if name not in self._clients:
            url = getattr(self._settings, f"{name}_service_url", None)
            if not url:
                raise ServiceUnavailableError(f"No URL configured for service '{name}'.")
            self._clients[name] = ServiceClient(
                url,
                name=name,
                service_name=self._settings.service_name,
                internal_secret=self._settings.internal_api_secret,
                timeout=self._settings.internal_request_timeout,
            )
        return self._clients[name]

    async def aclose(self) -> None:
        for client in self._clients.values():
            await client.aclose()
        self._clients.clear()
