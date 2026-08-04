"""A thin async Meilisearch client.

Written against the HTTP API rather than the official SDK for one reason: **failure
behaviour is the whole point of this service**, and it has to be ours. Every path
out of this class ends in either a value or a :class:`ServiceUnavailableError` —
never a bare ``httpx`` exception bubbling into the 500 handler, and never a request
without a timeout.

The rules, in order of how much they matter:

1. **Never 500.** A connection refused, a DNS failure, a read timeout and a 502
   from a proxy in front of Meilisearch are all the same thing to a caller:
   ``503 search_unavailable``. Browsing, buying and reading keep working.
2. **Never hang.** Connect and read timeouts are explicit on every call. A hung
   upstream would otherwise pin a worker until the client gave up.
3. **Never leak the key.** The master key travels in a header and appears in no
   log line, error message or URL.
"""

from __future__ import annotations

import time
from typing import Any

import httpx

from knowledgeos_core import ServiceUnavailableError, UpstreamError, get_logger
from knowledgeos_core.metrics import upstream_request_duration_seconds, upstream_requests_total
from settings import Settings

logger = get_logger(__name__)

#: Presented to clients when the engine is unreachable. Deliberately free of
#: internal detail — the diagnosis goes to the log, keyed by request id.
UNAVAILABLE_MESSAGE = (
    "Search is temporarily unavailable. Browsing and reading are unaffected — "
    "please try your search again shortly."
)


class MeiliClient:
    """Everything this service needs from Meilisearch, and nothing more."""

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._service = settings.service_name
        headers = {"User-Agent": f"knowledgeos-{settings.service_name}/search"}
        if settings.meilisearch_master_key:
            headers["Authorization"] = f"Bearer {settings.meilisearch_master_key}"
        self._client = client or httpx.AsyncClient(
            base_url=settings.meilisearch_url.rstrip("/"),
            headers=headers,
            timeout=httpx.Timeout(
                settings.meilisearch_timeout,
                connect=settings.meilisearch_connect_timeout,
            ),
            limits=httpx.Limits(max_connections=100, max_keepalive_connections=20),
            follow_redirects=False,
        )

    @property
    def client(self) -> httpx.AsyncClient:
        return self._client

    # ---- transport -------------------------------------------------------

    async def request(
        self,
        method: str,
        path: str,
        *,
        json: Any = None,
        params: dict[str, Any] | None = None,
        # A per-request HTTP timeout handed to httpx, not an asyncio cancellation
        # scope: the query path and the indexing path need very different budgets
        # against the same client.
        timeout: float | None = None,  # noqa: ASYNC109
    ) -> Any:
        """One HTTP call, with the platform's failure semantics applied."""
        started = time.perf_counter()
        try:
            response = await self._client.request(
                method,
                path,
                json=json,
                params=params,
                timeout=timeout if timeout is not None else self._settings.meilisearch_timeout,
            )
        except httpx.HTTPError as exc:
            upstream_requests_total.labels(
                service=self._service, upstream="meilisearch", status="error"
            ).inc()
            # type(exc).__name__ rather than str(exc): the message can contain the
            # full URL, and the URL can contain a key in a misconfigured deploy.
            logger.warning(
                "search.meilisearch_unreachable",
                method=method,
                path=path,
                error=type(exc).__name__,
            )
            raise ServiceUnavailableError(
                UNAVAILABLE_MESSAGE,
                code="search_unavailable",
                details={"dependency": "meilisearch"},
            ) from exc
        finally:
            upstream_request_duration_seconds.labels(
                service=self._service, upstream="meilisearch"
            ).observe(time.perf_counter() - started)

        upstream_requests_total.labels(
            service=self._service, upstream="meilisearch", status=str(response.status_code)
        ).inc()

        if response.status_code >= 500:
            # The engine is up but broken. Same user-facing outcome as unreachable.
            logger.warning("search.meilisearch_error", path=path, status_code=response.status_code)
            raise ServiceUnavailableError(
                UNAVAILABLE_MESSAGE,
                code="search_unavailable",
                details={"dependency": "meilisearch"},
            )
        if response.status_code == 404:
            return None
        if response.status_code >= 400:
            # A 4xx means *we* built a bad request — a filter on a non-filterable
            # attribute, say. That is our bug, so it is a 502 with the engine's
            # own error code kept for the log, not echoed as a user message.
            detail = _error_code(response)
            logger.error(
                "search.meilisearch_rejected",
                path=path,
                status_code=response.status_code,
                meili_code=detail,
            )
            raise UpstreamError(
                "The search engine rejected this request.",
                code="search_request_rejected",
                details={"meilisearch_code": detail},
            )
        if not response.content:
            return None
        return response.json()

    # ---- queries ---------------------------------------------------------

    async def search(
        self,
        index: str,
        body: dict[str, Any],
        *,
        timeout: float | None = None,  # noqa: ASYNC109 - httpx budget, not a cancel scope
    ) -> dict[str, Any]:
        result = await self.request("POST", f"/indexes/{index}/search", json=body, timeout=timeout)
        if result is None:
            # A missing index is an empty result set, not an error: the first
            # deploy runs before the first reindex, and an empty search page is a
            # far better outcome than a 503 on a healthy engine.
            logger.info("search.index_missing", index=index)
            return {"hits": [], "estimatedTotalHits": 0, "processingTimeMs": 0}
        return result  # type: ignore[no-any-return]

    async def multi_search(self, queries: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Several indexes in one round trip — how autocomplete stays fast."""
        result = await self.request("POST", "/multi-search", json={"queries": queries})
        if not result:
            return []
        return list(result.get("results", []))

    # ---- documents -------------------------------------------------------

    async def add_documents(
        self, index: str, documents: list[dict[str, Any]], *, primary_key: str = "id"
    ) -> dict[str, Any]:
        result = await self.request(
            "POST",
            f"/indexes/{index}/documents",
            json=documents,
            params={"primaryKey": primary_key},
            timeout=self._settings.meilisearch_index_timeout,
        )
        return result or {}

    async def delete_documents(self, index: str, document_ids: list[str]) -> dict[str, Any]:
        result = await self.request(
            "POST",
            f"/indexes/{index}/documents/delete-batch",
            json=document_ids,
            timeout=self._settings.meilisearch_index_timeout,
        )
        return result or {}

    async def delete_all_documents(self, index: str) -> dict[str, Any]:
        result = await self.request(
            "DELETE",
            f"/indexes/{index}/documents",
            timeout=self._settings.meilisearch_index_timeout,
        )
        return result or {}

    async def get_document(self, index: str, document_id: str) -> dict[str, Any] | None:
        return await self.request("GET", f"/indexes/{index}/documents/{document_id}")

    # ---- index administration -------------------------------------------

    async def create_index(self, index: str, *, primary_key: str = "id") -> dict[str, Any]:
        result = await self.request(
            "POST",
            "/indexes",
            json={"uid": index, "primaryKey": primary_key},
            timeout=self._settings.meilisearch_index_timeout,
        )
        return result or {}

    async def update_settings(self, index: str, config: dict[str, Any]) -> dict[str, Any]:
        result = await self.request(
            "PATCH",
            f"/indexes/{index}/settings",
            json=config,
            timeout=self._settings.meilisearch_index_timeout,
        )
        return result or {}

    async def index_stats(self, index: str) -> dict[str, Any] | None:
        return await self.request("GET", f"/indexes/{index}/stats")

    async def health(self) -> dict[str, Any]:
        """Readiness probe. Reports status; never raises."""
        try:
            response = await self._client.get("/health", timeout=self._settings.meilisearch_timeout)
        except httpx.HTTPError as exc:
            return {"status": "down", "error": type(exc).__name__}
        if response.status_code != 200:
            return {"status": "down", "error": f"http_{response.status_code}"}
        return {"status": "up"}

    async def aclose(self) -> None:
        await self._client.aclose()


def _error_code(response: httpx.Response) -> str:
    try:
        return str(response.json().get("code", "unknown"))
    except Exception:
        return "unknown"
