# Frontend integration spec — KnowledgeOS

**The frontend has been built.** It was built with Antigravity, not Lovable, against a
typed mock API layer. This page is no longer a build prompt: it is the contract for
switching that mock layer onto the real backend.

> The filename is historical. Renaming it to `FRONTEND_INTEGRATION.md` is a `git mv`
> and a grep for the old name — do it whenever it stops being useful to keep the link
> stable.

**Everything below is generated from the running services**, not from memory: 111
user-facing endpoints across seven services, plus 67 admin/ops endpoints and 49
HMAC-only internal routes the browser never sees.

---

## Where each side stands

| | Status |
|---|---|
| **Backend** | ✅ 10 services, 833 tests, 9 schemas / 63 tables, migrations verified |
| **Frontend** | ✅ Built against `src/lib/api/` mocks (`VITE_USE_MOCK_API=true`) |
| **Integration** | ⬜ Not yet done — this page is the contract for it |

Live gateway:

```
VITE_API_BASE_URL = https://gateway-production-c3e0.up.railway.app
```

Every path below is relative to it. The gateway routes `/v1/auth`, `/v1/books`,
`/v1/search`, `/v1/orders`, `/v1/notifications`, `/v1/ai` and the rest to the right
service. **Never call a service directly; there is only this one host.**

`GET /health` for liveness, `/docs` for the live aggregated OpenAPI — that is the
authoritative contract if anything here is ambiguous.

---

## What the frontend already assumes, and whether it is right

The mock layer was written against assumptions. Here is each one checked against the
implementation.

| Frontend assumption | Backend reality | |
|---|---|---|
| In-memory access token | Access token in the `POST /v1/auth/login` body | ✅ |
| `httpOnly` refresh cookie | `kos_refresh`, httpOnly, set by auth | ✅ |
| `X-CSRF-Token` header | Readable `kos_csrf` cookie **and** `csrf_token` in the login body | ✅ |
| Concurrent 401 refresh queue | Correct and necessary — refresh **rotates** the token | ✅ |
| `formatMoney(minor, currency)` | Money is an integer of the minor unit everywhere | ✅ |
| Cursor infinite scroll | `{ items, next_cursor, has_more }` | ✅ |
| Idempotency key per attempt | `Idempotency-Key: <uuid>` header | ✅ |
| CGST/SGST/IGST breakdown | `tax: { percent, cgst_minor, sgst_minor, igst_minor, total_minor, place_of_supply }` | ✅ |
| 2-step MFA challenge | `{ mfa_required: true, challenge_token }` → `POST /v1/auth/login/mfa` | ✅ |
| Error copy-to-clipboard request id | `error.request_id` on every failure | ✅ |
| "about N results" | ⚠️ **Search only.** The catalogue has no total | ⚠️ |
| `VITE_FEATURE_AI` build flag | ⚠️ Should be **runtime**: `GET /v1/ai/status` | ⚠️ |
| 10s debounced progress save | ⚠️ Sends a **delta**, not a total — see below | ⚠️ |

### The three that need a change

**1. The catalogue has no result count.** `GET /v1/books` returns
`{ items, next_cursor, has_more }` and deliberately no `total` — counting a filtered
catalogue costs a full scan on every page of an infinite scroll. Only `GET /v1/search`
returns a count, and it is `estimated_total`, from a capped scan. Render it as
"about N results" and never as an exact figure; it will disagree with a `COUNT(*)`.

If the catalogue page needs counts, pass `?include_facets=true` and read
`facets` — you pay for the count once instead of on every page.

**2. AI availability is a runtime fact, not a build-time flag.** A deployment with no
`ANTHROPIC_API_KEY` has a working AI service that returns 503, and the daily cost
ceiling can exhaust mid-day. `GET /v1/ai/status` answers honestly:

```json
{ "available": true, "providers": ["anthropic"], "model": "claude-sonnet-4-5-...",
  "budget_remaining_usd": 41.9, "cache_enabled": true, "moderation_enabled": true }
```

Gate the assistant drawer on `available`, fetched once per session. Keep
`VITE_FEATURE_AI` as a kill switch if you like, but `false` from either source should
hide the affordance — offering a button that always 503s is worse than not offering it.

**3. Reading progress `session_seconds` is a delta.** It is accumulated server-side and
never overwritten, so a replayed sync cannot rewrite total reading time:

```
PUT /v1/reading-progress/{book_id}
{ "position": "epubcfi(/6/14!/4/2/1:0)", "percent": 34.2,
  "page_number": 88, "session_seconds": 45 }
```

Send **seconds read since the last successful sync**, not the running total. Sending
the total makes `total_reading_seconds` grow quadratically. The response carries the
server's `total_reading_seconds`; treat that as authoritative and reset your local
counter on success.

---

## Conventions

**Errors.** Every failure, from every service:

```json
{ "error": { "code": "book_not_purchasable", "message": "Human readable.",
             "details": {}, "request_id": "01J..." } }
```

Show `message`. Never show `code` or `request_id` in normal UI — `request_id` belongs
in the copy-to-clipboard affordance on the error state, which the frontend already has.

**Money is always an integer of the currency's minor unit.** `price_minor: 49900` is
₹499.00. Never do float arithmetic on it.

**Cursor pagination** on the catalogue, search, orders, notifications and library:
`{ items, next_cursor, has_more }`, requested with `?cursor=<next_cursor>&limit=20`.
`next_cursor: null` means the end. **No totals, no page numbers.**

**Offset pagination** on the small near-static taxonomies only — authors, publishers,
categories — with `?page=1&limit=20` returning `{ items, meta: { page, limit, total, pages } }`.
Those are the only endpoints where a pager is the right control.

**Dates** are ISO 8601 UTC strings.

**Idempotency.** Send `Idempotency-Key: <uuid>` on `POST /v1/orders` and
`POST /v1/payments/verify`. Generate it once per checkout *attempt* and reuse it across
retries of that attempt — a new key on retry creates a second order. Reusing a key with
a *different* body is rejected with `idempotency_key_reuse`.

---

## Endpoint inventory

111 user-facing endpoints. Admin and operations endpoints (`/v1/admin/*`,
`/v1/automation/*`) are omitted — they need staff permissions and are not part of the
customer app.

### Auth — `auth` service

- `POST /v1/auth/register` — Create an account
- `POST /v1/auth/login` — Sign in with email and password
- `POST /v1/auth/login/mfa` — Complete a two-factor sign-in
- `POST /v1/auth/refresh` — Exchange a refresh token for a new access token
- `POST /v1/auth/logout` — Sign out of this device
- `POST /v1/auth/logout/all` — Sign out of every device
- `GET /v1/auth/me` — Get the signed-in user
- `PATCH /v1/auth/me` — Update your profile
- `POST /v1/auth/change-password` — Change your password
- `POST /v1/auth/forgot-password` — Request a password reset link
- `POST /v1/auth/reset-password` — Set a new password using a reset token
- `POST /v1/auth/verify-email` — Confirm an email address
- `POST /v1/auth/verify-email/resend` — Resend the verification email
- `GET /v1/auth/sessions` — List your active sessions
- `DELETE /v1/auth/sessions/{session_id}` — Revoke one session
- `GET /v1/auth/mfa/status` — Two-factor status
- `POST /v1/auth/mfa/enroll` — Begin two-factor enrolment
- `POST /v1/auth/mfa/confirm` — Confirm enrolment and activate two-factor
- `POST /v1/auth/mfa/disable` — Turn off two-factor authentication
- `POST /v1/auth/mfa/recovery-codes` — Regenerate recovery codes
- `GET /v1/auth/oauth/providers` — List configured OAuth providers
- `GET /v1/auth/oauth/{provider}/start` — Begin an OAuth sign-in
- `GET /v1/auth/oauth/{provider}/callback` — OAuth callback
- `GET /v1/auth/oauth/accounts` — List linked OAuth accounts
- `DELETE /v1/auth/oauth/accounts/{provider}` — Unlink an OAuth account
- `GET /v1/auth/permissions` — List the permissions each role grants

### Catalogue — `books` service

- `GET /v1/books` — Browse the catalogue
- `GET /v1/books/{slug}` — Get a book by slug
- `GET /v1/books/{slug}/related` — Books readers also liked
- `GET /v1/books/{book_id}/access` — Can the signed-in user read this book?
- `GET /v1/books/{book_id}/download` — Get a signed download URL
- `GET /v1/books/{book_id}/reviews` — List reviews for a book
- `POST /v1/books/{book_id}/reviews` — Write a review
- `PATCH /v1/reviews/{review_id}` — Edit your own review
- `DELETE /v1/reviews/{review_id}` — Delete your own review
- `POST /v1/reviews/{review_id}/vote` — Mark a review helpful or unhelpful

### Taxonomy — `books` service *(offset paginated)*

- `GET /v1/authors` · `GET /v1/authors/{slug}`
- `GET /v1/publishers` · `GET /v1/publishers/{slug}`
- `GET /v1/categories` · `GET /v1/categories/tree` · `GET /v1/categories/{slug}`

*(The `POST`/`PATCH`/`DELETE` variants on these exist but need `books:write`.)*

### Search — `search` service

- `GET /v1/search` — Search the catalogue
- `GET /v1/search/suggest` — Autocomplete
- `GET /v1/search/trending` — Trending searches
- `GET /v1/search/related/{book_id}` — Books similar to one book
- `POST /v1/search/click` — Record which result was opened

### Library and reading — `books` service

- `GET /v1/library` — Books the signed-in user can read
- `GET /v1/library/detailed` — Library with entitlement and reading progress
- `GET /v1/reading-progress` — Your reading progress across books
- `GET /v1/reading-progress/{book_id}` — Your progress in one book
- `PUT /v1/reading-progress/{book_id}` — Sync reading position
- `GET /v1/bookmarks` · `POST /v1/bookmarks`
- `PATCH /v1/bookmarks/{bookmark_id}` · `DELETE /v1/bookmarks/{bookmark_id}`
- `GET /v1/wishlist` · `POST /v1/wishlist` · `DELETE /v1/wishlist/{book_id}`
- `GET /v1/collections` · `POST /v1/collections`
- `GET /v1/collections/{collection_id}` · `PATCH` · `DELETE`
- `POST /v1/collections/{collection_id}/items`
- `DELETE /v1/collections/{collection_id}/items/{book_id}`

### Checkout and payments — `payment` service

- `POST /v1/checkout/quote` — Price a cart without creating an order
- `POST /v1/coupons/validate` — Check a coupon against a cart
- `POST /v1/orders` — Create an order and open a checkout session
- `GET /v1/orders` — Your order history
- `GET /v1/orders/{order_id}` — One of your orders *(poll this)*
- `POST /v1/orders/{order_id}/cancel` — Cancel an unpaid order
- `GET /v1/payments/providers` — Which gateways this deployment can use
- `POST /v1/payments/verify` — Confirm a payment from the browser
- `GET /v1/invoices` · `GET /v1/invoices/{invoice_id}`

### Subscriptions and affiliate — `payment` service

- `GET /v1/plans` — Available subscription plans
- `GET /v1/subscriptions` — Your subscriptions
- `GET /v1/subscriptions/active` — Your current membership, if any
- `POST /v1/subscriptions` — Start a subscription
- `POST /v1/subscriptions/{subscription_id}/cancel` — Cancel a subscription
- `POST /v1/affiliate` — Become an affiliate
- `GET /v1/affiliate/me` — Your affiliate account
- `GET /v1/affiliate/conversions` — Your referred sales

### Notifications — `notifications` service

- `GET /v1/notifications` — Your notifications *(cursor paginated)*
- `GET /v1/notifications/unread-count` — Unread badge count
- `POST /v1/notifications/read` — Mark notifications read
- `POST /v1/notifications/{notification_id}/archive` — Dismiss a notification
- `GET /v1/notifications/preferences` · `PUT /v1/notifications/preferences`
- `GET /v1/notifications/channels` — Channels this deployment can use
- `POST /v1/notifications/devices` · `DELETE /v1/notifications/devices/{device_id}`
- `POST /v1/notifications/unsubscribe` — One-click unsubscribe *(public, token in body)*

### AI assistant — `ai` service

- `GET /v1/ai/status` — Whether AI features are usable right now
- `POST /v1/ai/chat` — Ask the assistant
- `GET /v1/ai/conversations` — Your conversations
- `GET /v1/ai/conversations/{conversation_id}` — One conversation with its messages
- `DELETE /v1/ai/conversations/{conversation_id}` — Delete a conversation
- `POST /v1/ai/recommend` — Personalised recommendations

---

## Flows worth getting exactly right

### Sign-in with 2FA

```
POST /v1/auth/login  { email, password, device_label? }
  → 200 { access_token, token_type: "Bearer", expires_in,
          csrf_token, user }                            ← no 2FA, done
  → 200 { mfa_required: true, challenge_token,
          expires_in, methods: ["totp","recovery_code"] } ← show the code modal

POST /v1/auth/login/mfa  { challenge_token, code }
  → 200 { access_token, expires_in, csrf_token, user }
```

`code` is a 6-digit TOTP **or** a recovery code — one field, the server tells them
apart, and `methods` on the challenge says which are available. The challenge token is short-lived; on expiry, restart from `/login` rather than
asking for another code.

### Token refresh

`POST /v1/auth/refresh` with `credentials: 'include'` and the `X-CSRF-Token` header.
The value comes from either the readable `kos_csrf` cookie or the `csrf_token` field
in the last auth response — they are the same value, so take whichever is easier.
Omitting it on the cookie flow is a hard `csrf_missing` 401; it is only skipped for
non-browser clients that send the refresh token in the body, where CSRF does not apply.

The refresh token **rotates** on every use and reuse is treated as theft — a replayed
old token revokes the whole family and signs the user out everywhere. That is why the
concurrent-401 queue matters: two parallel refreshes race, the loser replays a consumed
token, and the user is logged out for no reason. Single-flight it.

### Checkout

```
POST /v1/checkout/quote   { items: [{book_id, quantity}], coupon_code? }
  → { lines, subtotal_minor, discount_minor, taxable_minor,
      tax: { percent, cgst_minor, sgst_minor, igst_minor, total_minor, place_of_supply },
      total_minor, currency, coupon_applied, coupon_message }

POST /v1/orders           { items, coupon_code?, provider? }   + Idempotency-Key
  → { order, provider, provider_order_id, publishable_key,
      checkout_url, client_secret }        ← flat, not a nested `checkout` object

  ...redirect or open the gateway widget...

POST /v1/payments/verify  { ...gateway payload... }            + Idempotency-Key
GET  /v1/orders/{id}      ← poll until status is paid or failed
```

**The cart never sends prices.** `OrderItemIn` has no price field on purpose — the
server prices everything, so a tampered client cannot buy a book for ₹1. Keep sending
only ids and quantities, which the existing `localStorage` cart already does.

Exactly one of `(cgst_minor + sgst_minor)` or `igst_minor` is non-zero: intra-state
supply splits into CGST+SGST, inter-state is IGST. Render whichever is non-zero rather
than showing three rows with a zero in them.

A zero-total order (100% coupon, or a free book) skips the gateway and is paid
instantly. The existing "0-amount instant" path is correct.

### Reading

`GET /v1/books/{book_id}/access` before opening the reader — it is the authoritative
answer, and the download URL will refuse without it. `GET /v1/books/{book_id}/download`
returns a **short-lived signed URL**; treat it as single-use and re-request rather than
caching it, since the link itself is the capability.

---

## Not yet wired: what the backend gained after the frontend was built

Four services landed after the mock layer was written. Three of them are invisible to
customers — automation, workers, admin — but two things are worth picking up:

**Feature flags.** The admin service now owns runtime feature flags with per-user
percentage rollouts. There is no public read endpoint by design (flag evaluation is
HMAC-only, service-to-service), so if the frontend needs them, the gateway would have
to expose a thin read-through. Worth doing before the next `VITE_FEATURE_*` variable is
added; not worth doing for one flag.

**AI status.** `GET /v1/ai/status`, described above. This is the one change worth
making now, because a build-time `VITE_FEATURE_AI` cannot know the daily budget ran out
at 3pm.

---

## The integration itself

```bash
# 1. Flip off the mocks — the URL is already correct
VITE_USE_MOCK_API=false

# 2. Allow the frontend origin on every service
CORS_ORIGINS=https://yourfrontend.app

# 3. Allow the checkout return URL on the payment service.
#    Origins, not hostnames — scheme included, no trailing slash.
ALLOWED_RETURN_ORIGINS=https://yourfrontend.app
```

Then, in order:

1. **`GET /health` and `/docs`** — confirm the gateway is up and the OpenAPI matches
   this page. If they disagree, `/docs` is right and this page is stale.
2. **Auth first.** Register, log in, refresh, log out. Nothing else works until the
   token lifecycle does, and the refresh-rotation behaviour is the most likely place
   for the mock and the real client to diverge.
3. **Catalogue and search.** Read-only, unauthenticated, and the cursor shape is
   identical to the mock — this should be the easy one. Watch for the missing
   catalogue `total`.
4. **Library and reading.** Check the `session_seconds` delta first; it is silent when
   wrong and only shows up as absurd reading times a week later.
5. **Checkout last**, against a test gateway. It is the only flow where a wrong
   assumption costs money rather than a re-render.

### One tradeoff worth knowing

The access token lives in memory, so a hard refresh logs the user out until the silent
refresh completes — a brief flash of the signed-out state on first paint. That is the
correct trade: a token in `localStorage` is readable by any XSS on the page, and this
one is not. Render a loading state on boot rather than the signed-out shell, and the
flash disappears.
