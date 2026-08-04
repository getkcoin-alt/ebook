# Book Service

The catalogue, the reader's shelf, and the entitlements that decide who may open
which file. **Highest read traffic on the platform** — design accordingly.

- **Port** 8002 · **Schema** `books` · **Depends on** PostgreSQL, Redis, S3/MinIO

## What it owns

Books, authors, categories (a tree), publishers, file versions, reviews and votes,
bookmarks, reading progress, wishlists, collections, and **entitlements**.

43 endpoints; see `/docs` outside production.

## The rules that shape this service

**Entitlement is checked before a URL is minted.** `GET /v1/books/{id}/download`
verifies access, *then* returns a short-lived presigned URL. The URL is the
capability — anyone holding it can read the object until it expires — so the check
cannot come after. The file never passes through this service; proxying a 40MB PDF
would occupy a worker for the whole transfer.

**402 vs 403 is a real distinction.** 402 means "buy this" and the UI should offer
to sell it. 403 means a grant exists but forbids the action (a read-only
subscription trying to download).

**Money is integer minor units.** `price_minor = 49900` is ₹499.00. There is no
float price column and there never will be — float money produces invoices that do
not reconcile.

**Ratings are denormalised.** `rating_average` / `rating_count` live on `books` and
are maintained on review writes. A catalogue page must never `AVG()` across reviews.

**Relationships are `lazy="raise_on_sql"`.** A relationship that lazy-loads inside a
list endpoint is an N+1 in production. Making it raise forces the query to declare
its `selectinload` up front, so the mistake surfaces in a test rather than in the p99.

**Cursor pagination everywhere public.** `OFFSET 40000` on a large catalogue is a
production incident. Keyset on `(sort_column, id)` stays O(limit) at any depth and
never skips or duplicates a row when a book is inserted mid-scroll. There is no
`total` on the catalogue feed: counting a filtered set costs a full scan on every
page and infinite scroll never displays the number.

**Sorting is a whitelist.** User input selects from a declared set; it is never
interpolated into SQL. `?sort_by=; DROP TABLE books` is a 400 listing the allowed
values.

## Entitlements arrive as events, not calls

Payment publishes `payment.succeeded` / `order.paid`; this service consumes them
(group `books`) and grants access. If payment called us synchronously while we were
mid-deploy, a customer would have paid for a book they cannot open.

Delivery is **at-least-once**, so the handler is idempotent twice over: a
`processed_events` ledger claims each event id, and `grant()` itself is idempotent
on `(user, book, source, external_ref)`. A redelivered event grants nothing new; a
re-grant after a revoked refund reinstates the same row rather than duplicating it.

## Caching

Book-detail-by-slug and the category tree are cached in Redis and invalidated on
write. The tree is assembled in Python from one flat SELECT rather than a recursive
CTE per request — depth is 2–3 and it changes far less often than it is read.

## Configuration

See [`.env.example`](.env.example). Two worth calling out:

- `DOWNLOAD_URL_TTL` (default 300s) — short on purpose; a link leaked in a
  screenshot should go stale quickly.
- `S3_PUBLIC_ENDPOINT_URL` must be what a **browser** resolves, not the internal
  hostname. Getting this wrong produces URLs that work in tests and 404 for users.

## Events

**Publishes** `book.created`, `book.updated`, `book.published`, `book.unpublished`,
`book.deleted`, `review.created` — search reindexes and the gateway purges its cache
off these.

**Consumes** `payment.succeeded`, `order.paid`.

## Running locally

```bash
cp .env.example .env
alembic upgrade head
python -m main          # :8002
```

## Tests

```bash
PYTHONPATH=apps/books pytest apps/books/tests -q
```

38 tests, no infrastructure — in-memory SQLite with the `books` schema ATTACHed,
plus `fakeredis` and a stub storage. Covers entitlement gating (402/403/expiry),
idempotent grants and re-grants, cursor pagination walking a full set without gaps
or duplicates, one-review-per-user, ownership isolation, and soft deletes.

Migration verified: 17 tables, `alembic check` reports no drift, downgrade clean.
