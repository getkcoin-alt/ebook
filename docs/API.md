# KnowledgeOS API

The complete HTTP contract for the deployed platform: **220 operations across 185
paths**, served through one gateway.

Everything on this page was read off the running system, not off the source. The
endpoint list is generated from the gateway's aggregated OpenAPI document; the
status codes and payloads in the examples are ones the deployed API actually
returned.

| | |
|---|---|
| **Base URL** | `https://api.allelearning.in` |
| **Web app** | `https://allelearning.in` (and `www.`) |
| **Machine-readable spec** | `GET /openapi.json` — every path, schema and enum |
| **Auth** | `Authorization: Bearer <access token>` (RS256 JWT) |
| **Content type** | `application/json` throughout, except presigned uploads |
| **Not deployed** | The automation service. Every `/v1/automation/*` path returns 503. |
| **Not configured** | Razorpay and Stripe credentials. See [Payments](#payments). |

Nothing below is aspirational. Where a capability is present but unconfigured, it
says so rather than describing what it would do.

---

## Contents

- [How the API is shaped](#how-the-api-is-shaped)
  - [Authentication](#authentication)
  - [Errors](#errors)
  - [Pagination](#pagination)
  - [Idempotency](#idempotency)
  - [Rate limits](#rate-limits)
  - [Caching](#caching)
- [Endpoint reference](#endpoint-reference)
  - [Auth and accounts](#auth-and-accounts)
  - [Catalogue](#catalogue)
  - [Library and reading](#library-and-reading)
  - [Reviews](#reviews)
  - [Payments](#payments)
  - [Search](#search)
  - [Notifications](#notifications)
  - [AI](#ai)
  - [Admin](#admin)
  - [Internal](#internal)
- [Worked example: catalogue to download](#worked-example-catalogue-to-download)

---

## How the API is shaped

One gateway fronts nine services. A client sees a single origin and never needs to
know which service answered — that is the point, and it is why the paths are grouped
by resource rather than by owning service. The gateway resolves routes by
**longest-prefix-wins**, so `/v1/books/{id}/download` can carry different auth,
caching and streaming rules from `/v1/books` without any ordering games.

### Authentication

Access tokens are **RS256 JWTs, valid for 15 minutes**. The auth service holds the
private key; every other service verifies offline against the public JWKS at
`GET /.well-known/jwks.json`. There is no introspection call on the hot path, so a
token check costs nothing and works during an auth-service restart.

```http
POST /v1/auth/login
Content-Type: application/json

{"email": "you@example.com", "password": "…"}
```

```json
{
  "access_token": "eyJhbGciOiJSUzI1NiIsImtpZCI6ImI3M2NhYTg4…",
  "refresh_token": "…",
  "token_type": "bearer",
  "expires_in": 900
}
```

Send it on every subsequent call:

```http
Authorization: Bearer eyJhbGciOiJSUzI1NiIs…
```

**Refresh tokens are opaque, last 30 days, and rotate on every use.** Presenting a
token that has already been exchanged is treated as theft, not as a mistake: the
whole token family is revoked and the user is signed out everywhere. Handle a 401
from `/v1/auth/refresh` by sending the user to sign in again, never by retrying.

The refresh token normally travels as an **httpOnly cookie**, and the endpoint
requires a CSRF token alongside it — a cookie the browser attaches automatically is
a cookie an attacker's page can cause to be attached too:

```
401  {"error": {"code": "csrf_missing", "message": "A CSRF token is required to refresh."}}
```

Non-browser clients (mobile, CLI, server-to-server) send the token in the body
instead and are not subject to the CSRF check:

```http
POST /v1/auth/refresh
{"refresh_token": "…"}
```

> **Why the API is on `api.allelearning.in` and not somewhere else.**
>
> The refresh and CSRF cookies are `SameSite=Strict`, which means the browser
> attaches them only to same-site requests. `allelearning.in` and
> `api.allelearning.in` share a registrable domain, so they are same-site and the
> cookies flow. Move the API to an unrelated host — `*.up.railway.app`, a different
> domain — and the browser silently stops sending them: login still appears to work,
> because the access token comes back in the body, and then every session dies at
> the fifteen-minute mark with no way to refresh.
>
> If the API ever has to live on a different registrable domain, the cookies must be
> changed to `SameSite=None; Secure`. That re-opens the CSRF surface `Strict` closes,
> and third-party cookie restrictions will break it again later. Keeping the API on a
> subdomain is the cheaper answer.

**Two-factor** is TOTP, enrolled at `POST /v1/auth/mfa/enroll` and confirmed at
`/mfa/confirm`. When a user has it enabled, `POST /v1/auth/login` does not return
tokens — it returns an MFA challenge, and the client completes the exchange at
`POST /v1/auth/login/mfa`.

**Roles** are `user`, `moderator`, `admin` and `superadmin`. `GET
/v1/auth/permissions` returns the exact permission set each role grants, so an
interface can hide what a user cannot do rather than guessing. Registration only
ever produces a `user`; the first `superadmin` is created by an operator CLI
(`python -m bootstrap`), never through the API.

### Errors

Every error, from every service, is the same envelope:

```json
{
  "error": {
    "code": "payment_required",
    "message": "You need to buy this book before you can download it.",
    "details": {"book_id": "b4eba151-2c9d-4245-a630-95f17615fcc5"},
    "request_id": "06e04b8667564bfb8fcb4e7986e790c5"
  }
}
```

`code` is stable and machine-readable — branch on it. `message` is human-facing and
safe to show. `request_id` also comes back as the `X-Request-ID` response header and
is the only thing worth quoting in a bug report. Unhandled exceptions never leak
their text in production.

| Status | `code` | Means |
|---|---|---|
| 400 | `bad_request` | Malformed input the schema could not describe — a bad cursor, an unsupported sort |
| 401 | `unauthorized` | No token, expired token, or wrong credentials |
| 402 | `payment_required` | The resource is real and purchasable, and you have not bought it |
| 403 | `forbidden` | Authenticated, but your role does not permit this |
| 404 | `resource_not_found` | No such resource — also returned for drafts, so unreleased slugs are not probeable |
| 409 | `conflict` | The action contradicts current state (publishing a book with no file) |
| 413 | `payload_too_large` | Body above 25 MB |
| 415 | `unsupported_media_type` | Content type not accepted for this route |
| 422 | `validation_error` | Failed schema validation; `details.fields` lists what and where |
| 429 | `rate_limited` | See [Rate limits](#rate-limits); `details.scope` names the bucket |
| 502 | `upstream_error` | A service answered, but with something the gateway could not use |
| 503 | `service_unavailable` | A dependency is unreachable or not deployed; the platform is degraded, not broken |

**402 versus 403 is deliberate.** A book you have not bought is a 402, because the
right response is to offer to sell it. A 403 means no amount of money will help.

### Pagination

Two styles, chosen per endpoint by what the data is.

**Cursor** — for anything large or actively growing (the catalogue, orders,
notifications). Stable under concurrent writes: no skipped or repeated rows when
something is inserted mid-walk.

```http
GET /v1/books?limit=20
```
```json
{"items": [...], "next_cursor": "eyJjcmVhdGVkX2F0IjoiMjAy…", "has_more": true}
```

Pass `next_cursor` back as `?cursor=`. Stop when `has_more` is false. Treat the
cursor as opaque — constructing one yields `400 bad_request`.

**Offset** — for small, near-static sets (categories, authors, publishers), where a
total count is genuinely useful.

```http
GET /v1/categories?limit=50&offset=0
```
```json
{"items": [...], "total": 37}
```

`limit` is capped server-side; asking for 100000 is a `422`, not a table scan.

### Idempotency

Any request that moves money or creates a durable side effect accepts an
`Idempotency-Key` header. Replaying the same key with the same body returns the
original response instead of acting twice:

```http
POST /v1/orders
Idempotency-Key: 8b1f0e3a-6b3d-4d4d-9a55-1f2d3e4a5b6c
```

Same key with a *different* body is a `409 conflict` — that is a client bug, and
silently returning the first response would hide it. Use a fresh UUID per user
intent and reuse it across retries of that intent.

### Rate limits

Sliding window, applied at the gateway, per user when authenticated and per IP when
not. `429` responses name the bucket in `details.scope`.

| Scope | Limit | Window | Applies to |
|---|---|---|---|
| `anonymous` | 60 | 1 min | Unauthenticated reads |
| `authenticated` | 600 | 1 min | Signed-in default |
| `login` | 10 | 15 min | `/v1/auth/login`, `/refresh`, `/oauth` |
| `register` | 5 | 1 hour | `/v1/auth/register` |
| `password_reset` | 5 | 1 hour | `/forgot-password`, `/reset-password`, `/verify-email` |
| `search` | 120 | 1 min | `/v1/search` |
| `ai` | 30 | 1 min | `/v1/ai/*` |
| `checkout` | 20 | 5 min | `/v1/orders`, `/v1/payments` |
| `upload` | 20 | 1 hour | Presigned upload targets |
| `webhook` | 1000 | 1 min | Provider callbacks |

The credential buckets are tight on purpose — they are the brute-force surface. If
you are building an integration test suite, expect to hit `login` and `register`
first, and reuse accounts rather than minting one per run.

### Caching

The gateway caches successful `GET`s for a small number of routes and invalidates
them on domain events rather than waiting for a TTL to lapse.

| Route | TTL | Invalidated by |
|---|---|---|
| `/v1/books` | 120 s | `book.published`, `book.updated`, `book.unpublished`, `book.deleted` |
| `/v1/authors` | 300 s | `book.published`, `book.updated` |
| `/v1/categories` | 600 s | `book.published`, `book.updated`, `book.deleted` |
| `/v1/reviews` | 60 s | `review.created`, `review.deleted` |
| `/v1/library` | 30 s | per-user key |
| `/v1/search/trending` | 60 s | — |
| `/.well-known/jwks.json` | 300 s | — |

Anything keyed per user includes the user id in the cache key, so one reader's
library can never be served to another. **`/v1/search` is deliberately not cached**:
a cached hit never reaches the search service, which would undercount exactly the
queries the analytics exist to measure and would invert the trending ranking.

---

## Endpoint reference

`✱` marks routes that require authentication. `admin` marks routes gated on a staff
permission. Everything else is public.

### Auth and accounts

| | Endpoint | |
|---|---|---|
| `POST` | `/v1/auth/register` | Create an account |
| `POST` | `/v1/auth/login` | Sign in with email and password |
| `POST` | `/v1/auth/login/mfa` | Complete a two-factor sign-in |
| `POST` | `/v1/auth/refresh` | Exchange a refresh token for a new access token |
| `POST` | `/v1/auth/logout` ✱ | Sign out of this device |
| `POST` | `/v1/auth/logout/all` ✱ | Sign out of every device |
| `GET` | `/v1/auth/me` ✱ | Get the signed-in user |
| `PATCH` | `/v1/auth/me` ✱ | Update your profile |
| `POST` | `/v1/auth/change-password` ✱ | Change your password |
| `POST` | `/v1/auth/forgot-password` | Request a password reset link |
| `POST` | `/v1/auth/reset-password` | Set a new password using a reset token |
| `POST` | `/v1/auth/verify-email` | Confirm an email address |
| `POST` | `/v1/auth/verify-email/resend` | Resend the verification email |
| `GET` | `/v1/auth/sessions` ✱ | List your active sessions |
| `DELETE` | `/v1/auth/sessions/{session_id}` ✱ | Revoke one session |
| `GET` | `/v1/auth/permissions` | What each role may do |
| `GET` | `/.well-known/jwks.json` | Public signing keys |

**Two-factor** — `GET /v1/auth/mfa/status` ✱, `POST /v1/auth/mfa/enroll` ✱,
`/mfa/confirm` ✱, `/mfa/disable` ✱, `/mfa/recovery-codes` ✱.

**OAuth** — `GET /v1/auth/oauth/providers`, `/oauth/{provider}/start`,
`/oauth/{provider}/callback`, `GET /v1/auth/oauth/accounts` ✱,
`DELETE /v1/auth/oauth/accounts/{provider}` ✱. Google and GitHub are supported;
neither has credentials configured on this deployment, so `providers` returns an
empty list rather than offering a button that cannot work.

> **Registration never reveals whether an address exists.** `/register`,
> `/forgot-password` and `/login` return identical responses — and take comparable
> time — for known and unknown addresses. Do not build a "is this email taken?"
> check on top of them; there isn't one, on purpose.

### Catalogue

Reads are public. Writes are staff-only and live under `/v1/admin/books`.

| | Endpoint | |
|---|---|---|
| `GET` | `/v1/books` | Browse the catalogue — cursor paged, faceted |
| `GET` | `/v1/books/{slug}` | Get a book by slug |
| `GET` | `/v1/books/{slug}/related` | Books readers also liked |
| `GET` | `/v1/books/{book_id}/access` ✱ | Can the signed-in user read this? |
| `GET` | `/v1/books/{book_id}/download` ✱ | Signed download URL — entitlement gated |
| `GET` | `/v1/categories` · `/categories/tree` · `/categories/{slug}` | Categories |
| `GET` | `/v1/authors` · `/authors/{slug}` | Authors |
| `GET` | `/v1/publishers` · `/publishers/{slug}` | Publishers |

Filters on `/v1/books`: `q`, `category`, `author`, `language`, `format`,
`min_price_minor`, `max_price_minor`, `free_only`, `sort_by`, `limit`, `cursor`.
`sort_by` selects from a closed set — an unrecognised value is a `400` listing what
is allowed, never an interpolation into SQL.

**Drafts are 404, not 403.** Confirming that a slug exists would leak an unreleased
title. Staff see drafts at the same canonical URL.

**Prices are integers in the currency's minor unit** — `price_minor: 49900` is
₹499.00. There is no float anywhere in the money path, and the client never sends a
price: orders carry book ids and the server prices them from the catalogue.

Staff writes:

| | Endpoint | |
|---|---|---|
| `GET`/`POST` | `/v1/admin/books` `admin` | List including drafts / create |
| `GET`/`PATCH`/`DELETE` | `/v1/admin/books/{book_id}` `admin` | Read / update / soft-delete |
| `POST` | `/v1/admin/books/uploads` `admin` | Mint a presigned upload target |
| `POST` | `/v1/admin/books/{book_id}/versions` `admin` | Record a new file version |
| `POST` | `/v1/admin/books/{book_id}/publish` `admin` | Publish |
| `POST` | `/v1/admin/books/{book_id}/unpublish` `admin` | Unpublish |
| `POST`/`PATCH`/`DELETE` | `/v1/authors`, `/v1/categories`, `/v1/publishers` `admin` | Taxonomy |

**Publishing is refused until a readable file exists:**

```
409  {"error": {"code": "conflict",
                "message": "This book has no uploaded file, so it cannot be published."}}
```

A store listing that sells something nobody can open is worse than no listing.

### Library and reading

All require authentication.

| | Endpoint | |
|---|---|---|
| `GET` | `/v1/library` ✱ | Books the user can read |
| `GET` | `/v1/library/detailed` ✱ | With entitlement and progress |
| `GET`/`POST` | `/v1/wishlist` ✱ | Read / add |
| `DELETE` | `/v1/wishlist/{book_id}` ✱ | Remove |
| `GET`/`POST` | `/v1/bookmarks` ✱ | Read / create |
| `PATCH`/`DELETE` | `/v1/bookmarks/{bookmark_id}` ✱ | Edit / delete |
| `GET` | `/v1/reading-progress` ✱ | Progress across books |
| `GET`/`PUT` | `/v1/reading-progress/{book_id}` ✱ | Read / sync position |
| `GET`/`POST` | `/v1/collections` ✱ | Read / create |
| `GET`/`PATCH`/`DELETE` | `/v1/collections/{collection_id}` ✱ | Manage |
| `POST` | `/v1/collections/{collection_id}/items` ✱ | Add a book |
| `DELETE` | `/v1/collections/{collection_id}/items/{book_id}` ✱ | Remove a book |

`GET /v1/books/{book_id}/access` is the question a Read button should ask:

```json
{"book_id": "75bd6bf0-4ab3-443b-bafb-d38eb6ab77e4",
 "has_access": false, "can_download": false, "source": null, "expires_at": null}
```

The field is **`has_access`**, not `can_read`. `source` is `purchase`,
`subscription` or `free` once access exists.

**Downloads are gated on that entitlement, checked before the URL is minted.**
Without one:

```
402  {"error": {"code": "payment_required", …}}
```

With one, you get a short-lived signed URL:

```json
{"book_id": "75bd6bf0-…", "format": "epub",
 "url": "https://srv1628639.hstgr.cloud:9000/knowledgeos/private/book/…?X-Amz-Signature=…",
 "filename": "flow-book-bd95c678.epub",
 "expires_in": 900, "expires_at": "2026-08-06T03:14:22Z"}
```

The URL is single-purpose and expires in 15 minutes. Do not cache or share it. The
file never passes through the API — proxying a 40 MB PDF would occupy a worker for
the whole transfer.

`?format=` is **optional**. Omit it and the book's preferred available format is
served (PDF, then EPUB, then MOBI). Name one the book does not have and you get a
`404` whose `details.available` lists what it does have.

A subscription grants a **read-only** entitlement: `access` reports
`has_access: true` with `can_download: false`, and `/download` is a `403`. Reading
progress requires an entitlement too — `PUT /v1/reading-progress/{book_id}` on an
unowned book is a `402`.

### Reviews

| | Endpoint | |
|---|---|---|
| `GET`/`POST` | `/v1/books/{book_id}/reviews` | List / write ✱ |
| `PATCH`/`DELETE` | `/v1/reviews/{review_id}` ✱ | Edit / delete your own |
| `POST` | `/v1/reviews/{review_id}/vote` ✱ | Mark helpful or unhelpful |
| `POST` | `/v1/reviews/{review_id}/moderate` `admin` | Approve or reject |
| `GET` | `/v1/admin/moderation/reviews` `admin` | Queue |
| `GET` | `/v1/admin/moderation/reviews/counts` `admin` | Counts per state |

### Payments

> **Razorpay and Stripe credentials are not set on this deployment.**
> `GET /v1/payments/providers` returns
> `{"providers": [], "default": null, "currency": "INR", "razorpay_key_id": null,
> "stripe_publishable_key": null}`. Order creation, pricing, GST,
> coupons, invoicing, refunds and entitlement granting all work; what is missing is
> the hosted card page. Settle an order with
> `POST /v1/admin/orders/{order_id}/mark-paid` (provider `manual`) and every
> downstream effect fires exactly as it would after a real capture.
>
> This is a safe state, not an unfinished one: the webhook handlers treat a missing
> signing secret as *refuse the delivery*, so nobody can mark an order paid by
> guessing the URL.

| | Endpoint | |
|---|---|---|
| `POST` | `/v1/checkout/quote` | Price a cart without creating an order |
| `POST` | `/v1/coupons/validate` | Check a coupon against a cart |
| `GET` | `/v1/payments/providers` | Which gateways this deployment can use |
| `POST` | `/v1/orders` ✱ | Create an order and open a checkout session |
| `GET` | `/v1/orders` ✱ | Your order history |
| `GET` | `/v1/orders/{order_id}` ✱ | One order |
| `POST` | `/v1/orders/{order_id}/cancel` ✱ | Cancel an unpaid order |
| `POST` | `/v1/payments/verify` ✱ | Confirm a payment from the browser |
| `GET` | `/v1/invoices` · `/v1/invoices/{invoice_id}` ✱ | Your GST invoices |
| `GET`/`POST` | `/v1/subscriptions` ✱ | List / start |
| `GET` | `/v1/subscriptions/active` ✱ | Current membership |
| `POST` | `/v1/subscriptions/{subscription_id}/cancel` ✱ | Cancel |
| `GET` | `/v1/plans` | Available plans |
| `POST` | `/v1/affiliate` ✱ | Become an affiliate |
| `GET` | `/v1/affiliate/me` · `/affiliate/conversions` ✱ | Your account and referred sales |
| `POST` | `/v1/webhooks/razorpay` · `/webhooks/stripe` | Provider callbacks |

Quote first, then order — the quote is the number to show, and it is computed the
same way the order will be:

```http
POST /v1/checkout/quote
{"items": [{"book_id": "…", "quantity": 1}], "currency": "INR"}
```

```json
{"lines": [{"book_id": "75bd6bf0-4ab3-443b-bafb-d38eb6ab77e4",
            "title": "The Smoke Test Handbook", "slug": "flow-book-bd95c678",
            "quantity": 1, "unit_price_minor": 49900, "line_total_minor": 49900,
            "already_owned": false}],
 "subtotal_minor": 49900, "discount_minor": 0, "taxable_minor": 42288,
 "tax": {"percent": 18, "cgst_minor": 3806, "sgst_minor": 3806,
         "igst_minor": 0, "total_minor": 7612, "place_of_supply": "GJ"},
 "total_minor": 49900, "currency": "INR",
 "coupon_code": null, "coupon_applied": false, "coupon_message": null}
```

**Listed prices are GST-inclusive.** Note that `total_minor` equals
`subtotal_minor`: ₹499.00 is what the customer pays, of which ₹76.12 is tax and
₹422.88 is the taxable value. Do not add `tax.total_minor` to `total_minor` — that
would charge the tax twice. Show `total_minor` as the price and the `tax` object as
a breakdown.

`already_owned` is per line and worth surfacing: it is how you stop someone buying a
book they have already paid for.

**GST is integer arithmetic throughout**: CGST+SGST within a state, IGST across
states, zero-rated for export, rounded once on the order total. Invoice numbers are
a consecutive serial within the Indian financial year, as the law requires.

Order creation returns a **`CheckoutSession` wrapping the order**, not the order
itself — the browser needs the provider handoff alongside it:

```json
{"order": {"id": "…", "status": "awaiting_payment", "total_minor": 49900, …},
 "provider": "manual", "provider_order_id": null, "publishable_key": null,
 "checkout_url": null, "client_secret": null,
 "amount_minor": 49900, "currency": "INR", "expires_at": "…"}
```

Read the order id from `response.order.id`. With no gateway configured,
`checkout_url` and `client_secret` are `null` and `provider` is `manual`.

**Confirmations arrive more than once, and that is fine.** Settlement is idempotent
through a unique `(provider, provider_payment_id)`, a `mark_paid()` that returns
false on an already-settled order, and downstream effects that are each idempotent
on their own. Nothing is ever hard-deleted: a refund is a new row plus a status
change.

Entitlements are granted by **consuming `payment.succeeded` from the event bus**,
not by a synchronous call from payment to books. If payment called books directly
and books happened to be mid-deploy, a customer would have paid for a book they
cannot open. Expect a **second or two** between settlement and
`/v1/books/{id}/access` reporting `can_read: true`; poll it, do not assume.

### Search

| | Endpoint | |
|---|---|---|
| `GET` | `/v1/search` | Search the catalogue |
| `GET` | `/v1/search/suggest` | Autocomplete across books, authors and categories |
| `GET` | `/v1/search/related/{book_id}` | Similar books |
| `GET` | `/v1/search/trending` | Trending searches |
| `POST` | `/v1/search/click` | Record which result was opened |

```json
{"query": "smoke", "hits": [...], "estimated_total": 0, "took_ms": 33,
 "facets": {"categories": [], "authors": [], "formats": [], "tags": [],
            "languages": [], "price_ranges": []},
 "next_cursor": null, "has_more": false, "did_you_mean": null,
 "semantic": false, "query_id": "e015556d-6115-4347-b1bc-94fd8ea0dcc8"}
```

Post the `query_id` back to `/v1/search/click` when a user opens a result; that is
what makes relevance measurable.

`status = "published"` is appended to every query unconditionally, so search can
never surface a draft. Filters and sorts are validated against closed sets *before*
reaching the engine, so a crafted query is a `400` from the platform rather than a
`500` leaking out of Meilisearch.

**A search outage is not a platform outage.** If Meilisearch is unreachable you get
a clean `503` on the query path and nothing else changes — browsing, buying and
reading are unaffected:

```
503  {"error": {"code": "search_unavailable",
                "message": "Search is temporarily unavailable. Browsing and reading are unaffected — please try your search again shortly."}}
```

`semantic` is `false` on this deployment: semantic search sits behind a config flag
and needs an embedding provider, which Groq does not offer. A request that opts in
without one gets keyword results and an honest `"semantic": false` rather than a
silent downgrade.

### Notifications

| | Endpoint | |
|---|---|---|
| `GET` | `/v1/notifications` ✱ | Your notifications |
| `GET` | `/v1/notifications/unread-count` ✱ | Badge count |
| `POST` | `/v1/notifications/read` ✱ | Mark read |
| `POST` | `/v1/notifications/{notification_id}/archive` ✱ | Dismiss |
| `GET`/`PUT` | `/v1/notifications/preferences` ✱ | Per-category preferences |
| `GET` | `/v1/notifications/channels` | Channels this deployment can use |
| `POST` | `/v1/notifications/devices` ✱ | Register a push token |
| `DELETE` | `/v1/notifications/devices/{device_id}` ✱ | Deregister |
| `POST` | `/v1/notifications/unsubscribe` | One-click unsubscribe |

Messages are **driven by events, not by callers**. A user registers, an order is
paid, a refund is issued — this service decides those facts deserve a message. There
is no "send email" call for other services to make, because then changing a subject
line would be a five-service deploy.

`GET /v1/notifications/channels` is worth calling before rendering a preferences
screen:

```json
{"channels": ["in_app", "email"], "sending_enabled": true, "email_provider": "smtp"}
```

Render toggles only for the channels in that array. On this deployment **email is
configured over SMTP and SMS, WhatsApp and push are not**, so offering those toggles
would promise something that cannot be delivered.

One rule outranks the rest: **a suppressed address is never contacted again**,
transactional or not, and removal is an operator action.

> **Email currently reaches Gmail and is refused there.** The path itself works end
> to end: the service authenticates over SMTP, postfix accepts and queues the
> message, and it is delivered to Gmail's servers. Gmail then rejects it:
>
> ```
> 550-5.7.26 Your email has been blocked because the sender is unauthenticated.
> 550-5.7.26 Gmail requires all senders to authenticate with either SPF or DKIM.
> ```
>
> SPF and DKIM are DNS records on the sending domain. Mail currently leaves as
> `@srv1628639.hstgr.cloud`, whose DNS is not ours to edit, so no application change
> fixes this. Two ways out, in order of preference:
>
> 1. **Use a transactional provider.** The service already implements one — set
>    `EMAIL_PROVIDER=resend` and `RESEND_API_KEY` on the notification service, and
>    delivery, SPF, DKIM and bounce webhooks come with it. No code change.
> 2. **Point a domain you control at the host**, publish SPF and DKIM records for
>    it, and set `FROM_EMAIL` to an address on that domain.
>
> Until one of those is done, treat email as queued but undeliverable to Gmail,
> Yahoo and Outlook. In-app notifications are unaffected.

### AI

| | Endpoint | |
|---|---|---|
| `GET` | `/v1/ai/status` ✱ | Whether AI features are usable right now |
| `POST` | `/v1/ai/chat` ✱ | Ask the assistant (streams) |
| `POST` | `/v1/ai/recommend` ✱ | Personalised recommendations |
| `GET` | `/v1/ai/conversations` ✱ | Your conversations |
| `GET`/`DELETE` | `/v1/ai/conversations/{conversation_id}` ✱ | Read / delete |

Backed by **Groq** (`llama-3.3-70b-versatile`) through the OpenAI-compatible client.

**Call `/v1/ai/status` before showing an AI affordance.** There is a hard daily cost
ceiling — $10 platform-wide, $1 per user — checked before every model call, and
reaching it returns `503` rather than continuing to spend. A chat button that
returns 503 is worse than one that was never rendered.

Chat is grounded in real catalogue results and verifies entitlement before any book
text reaches a prompt. Prompts are server-owned per task; user content only ever
occupies the user turn. Moderation fails closed.

Responses stream, and the gateway does not buffer them. Read incrementally.

### Admin

Every admin route requires a staff permission; a `user` token gets `403`. Call
`GET /v1/auth/permissions` to learn what the signed-in role may do and render
accordingly.

**Platform** — `GET /v1/admin/dashboard` (metrics assembled from every service),
`GET /v1/admin/health-board` (liveness across every service).

The dashboard fans out concurrently and **each panel carries its own status and
timeout**. One service restarting produces one unavailable card, not a red page:

```json
{"generated_at": "2026-08-05T18:09:54.773061Z", "window_days": 7,
 "panels": [{"service": "books", "status": "ok", "data": {…}},
            {"service": "automation", "status": "unavailable", "data": null}]}
```

Render `status` per panel. `automation` is permanently `unavailable` here.

**Users** — `GET /v1/admin/users`, `/users/stats`, `/users/{user_id}`,
`POST /users/{user_id}/ban`, `/unban`.

**Catalogue** — see [Catalogue](#catalogue). Also `POST /v1/admin/entitlements`
(grant access by hand) and `DELETE /v1/admin/entitlements/{entitlement_id}`.

**Commerce** — `GET /v1/admin/orders`, `/orders/{order_id}`,
`POST /orders/{order_id}/mark-paid`, `/orders/{order_id}/refund`,
`GET /orders/{order_id}/refunds`, `GET /v1/admin/revenue`,
`GET /v1/admin/invoices?user_id=…` (the `user_id` query parameter is required),
coupon CRUD at `/v1/admin/coupons`, plan CRUD at `/v1/admin/plans`,
`GET /v1/admin/webhooks` and `POST /v1/admin/webhooks/{id}/replay`.

Revenue is **gross minus refunds minus tax**. GST is collected on the government's
behalf and is not revenue; counting it would overstate the business.

**Feature flags** — `GET`/`POST` `/v1/admin/flags`, `GET`/`PATCH`/`DELETE`
`/v1/admin/flags/{key}`, `GET /v1/admin/flags/{key}/audit`.

Evaluation returns **decisions, not rules**. Services receive "on" or "off", never a
rollout percentage — two independent bucketing implementations diverge into a user
who has a feature on one page and not the next. Bucketing is a stable hash of flag
key and user id, so raising a rollout only ever adds people.

**Search** — `GET /v1/admin/search/health`, `/search/analytics`, `/search/runs`,
`POST /search/reindex`, `DELETE /search/trending`.

**Notifications** — template CRUD at `/v1/admin/notifications/templates` (plus
`/{id}/preview`, which renders without sending), `GET /notifications/deliveries`,
`/notifications/stats`, and suppression management at `/notifications/suppressions`.

Templates are `{{name}}` substitution and nothing more. They are editable through
this API, so a template language with arbitrary evaluation would be remote code
execution behind an admin token. A missing variable aborts the send: a customer
receiving `Hi {{first_name}},` is an apology, a `422` is a bug report.

**AI** — `GET /v1/admin/ai/usage`, `/ai/spend`, `/ai/failures`.

**Scheduler** — `GET /v1/admin/workers/health`, `/workers/jobs`,
`/workers/jobs/{job_name}`, `/workers/runs`, `POST /workers/jobs/{job_name}/run`.

`/workers/runs` answers the question logs are worst at: **has the scheduler stopped
firing?** A job that is not running produces no logs and no failures, so it is
invisible in every signal except the absence of recent runs. Losing the single-flight
lock is recorded as its own outcome and is not a failure — beat fires on every
replica, and a three-replica deployment must not look like it is failing two runs in
three.

**Audit** — `GET /v1/admin/audit-logs`, `/audit-logs/actions`.

### Internal

39 paths under `/internal/*` exist for service-to-service calls. They are **not
reachable through the public gateway** and require an HMAC signature over
`{timestamp}.{METHOD}.{path}.{body_sha256}` in `X-Internal-Signature`, with a
300-second freshness window. Private-network reachability is not authorisation.

They are listed in `/openapi.json` for completeness. Do not build against them.

---

## Worked example: catalogue to download

The whole ecommerce path, exactly as verified against the deployed platform.

**1. Operator publishes a book.** Create it, upload a file, attach it, publish.

```http
POST /v1/admin/books
Authorization: Bearer <staff token>

{"title": "The Smoke Test Handbook", "slug": "smoke-test-handbook",
 "price_minor": 49900, "currency": "INR", "language": "en",
 "author_ids": [{"author_id": "…", "role": "author"}]}
```
→ `201`, book is a **draft**.

```http
POST /v1/admin/books/uploads
{"category": "book", "filename": "handbook.epub", "content_type": "application/epub+zip"}
```
→ `200 {"url": "http://…/knowledgeos", "key": "private/book/…/handbook.epub", "fields": {…}}`

`POST` the file to `url` as multipart form data with `fields` included. Bytes go
straight to object storage and never pass through the API.

```http
POST /v1/admin/books/{book_id}/versions
{"epub_key": "private/book/…/handbook.epub", "file_size_bytes": 995}
```

```http
POST /v1/admin/books/{book_id}/publish
```
→ `200`. Before the upload this was `409` — no readable file.

**2. A customer finds it.** `GET /v1/books` now includes it, and
`GET /v1/books/{slug}` returns the detail. Search picks it up within a few seconds,
over the event bus.

**3. The gate holds.**

```http
GET /v1/books/{book_id}/download
Authorization: Bearer <customer token>
```
→ `402 payment_required`. Nothing has been bought.

**4. Purchase.** Quote, then order:

```http
POST /v1/orders
Idempotency-Key: <uuid>

{"items": [{"book_id": "…", "quantity": 1}], "currency": "INR",
 "provider": "manual", "billing_email": "…", "billing_name": "…"}
```
→ `201`, a `CheckoutSession` whose `order.status` is `awaiting_payment`, with GST
computed server-side. The order id is `response.order.id`.

With a live gateway the client would complete payment and the provider webhook would
settle it. Here an operator does:

```http
POST /v1/admin/orders/{order_id}/mark-paid
```

**5. Entitlement propagates.** Settlement publishes `payment.succeeded`; the books
service consumes it and writes an entitlement. Poll:

```http
GET /v1/books/{book_id}/access
```
→ `{"has_access": true, "can_download": true, "source": "purchase"}` — observed at
**2 seconds** after settlement on this deployment.

**6. Download works.**

```http
GET /v1/books/{book_id}/download
```
→ `200 {"format": "epub", "url": "…", "filename": "…", "expires_in": 900}`. No
`?format=` was sent and the book has only an EPUB, so an EPUB is what it serves.
Fetching that URL returns the **exact bytes uploaded in step 1** — verified
byte-for-byte.

The book is now in `GET /v1/library`, reading progress can be synced, and a GST
invoice is available at `GET /v1/invoices`.

---

## Health and status

| Endpoint | Answers |
|---|---|
| `GET /health` | Is this process alive? Restarting fixes a failure here. |
| `GET /health/ready` | Are dependencies reachable? Degradation, not a restart signal. |
| `GET /openapi.json` | The full machine-readable specification. |

```http
GET /health/ready
```
```json
{"status": "ready", "service": "gateway",
 "dependencies": {"redis": {"status": "up"},
                  "upstream:books": {"status": "up"},
                  "upstream:automation": {"status": "down", "error": "ConnectError"}}}
```

`upstream:automation` is expected to be `down` — that service is not deployed.
Everything else should read `up`.
