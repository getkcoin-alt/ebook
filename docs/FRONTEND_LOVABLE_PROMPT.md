# Lovable prompt — KnowledgeOS frontend

Everything between the two `═══` rules is the prompt. Paste it into Lovable as the
first message of a new project.

**Read this page's last section ("After Lovable is done") before you paste** — it
lists the three things that make integration a config change rather than a rewrite.

---

═══════════════════ COPY FROM HERE ═══════════════════

Build **KnowledgeOS** — the customer-facing web app for a premium e-book store and
reader.

## The single most important instruction

**Do not create a backend. Do not enable Supabase. Do not enable a database, auth,
storage, or edge functions.**

A complete REST API already exists and is deployed. Your job is the frontend only.
Every piece of data comes from that API over HTTP. If you add Supabase auth or a
database table, the work has to be thrown away.

All API access goes through **one base URL** from an environment variable, which is
already live:

```
VITE_API_BASE_URL = https://gateway-production-c3e0.up.railway.app
```

That is the API gateway. Every path in this prompt is relative to it — the gateway
routes `/v1/auth`, `/v1/books`, `/v1/search`, `/v1/orders`, `/v1/checkout`,
`/v1/notifications` and the rest to the right service behind it. **Never call a
service directly; there is only this one host.**

Check it with `GET /health` and read the live, aggregated API documentation at
`/docs` — that is the authoritative contract if anything below is ambiguous.

Build against mocks first anyway, so a backend hiccup never blocks UI work:

- Put every network call behind a typed client in `src/lib/api/`.
- Add `src/lib/api/mock/` with realistic fixture data.
- Switch between them with `VITE_USE_MOCK_API` (default `true`).
- **The mock adapter and the real adapter must implement the same TypeScript
  interface**, so flipping the flag is the entire integration.

Never call `fetch` directly from a component. Components call hooks; hooks call the
client; the client picks mock or real.

## Stack

React + TypeScript + Tailwind + shadcn/ui (Lovable's default). Add TanStack Query for
server state and React Router for routing. No state management library beyond
Query + React context.

---

## The design bar

This should feel like **Linear, Raycast and Apple Books** — not like a template.
Specifically:

- **Restraint.** Lots of whitespace, few borders, one accent colour. No gradients on
  buttons, no drop shadows on cards, no emoji in the UI.
- **Type is the design.** A real typographic scale. Book titles are the loudest thing
  on any page. Use `Inter` for UI and a serif (`Source Serif 4`) for reading.
- **Motion is functional.** 150–200ms ease-out on state changes. Things that appear
  should fade + translate 4px, not bounce or scale.
- **Dark mode is not an afterthought.** Design it first; light mode second. Persist
  the choice, respect `prefers-color-scheme` on first visit.
- **Every list has three states**: loading (skeletons matching final layout, never a
  spinner), empty (an illustration + one sentence + one action), and error (what
  failed + a retry button).
- **Keyboard first.** Visible focus rings everywhere. Full keyboard navigation.

### Command palette (⌘K / Ctrl+K)

The signature interaction. Opens instantly, over everything. Sections: recent
searches, book results (live as you type), quick actions (Go to library, Toggle
theme, Account settings), and navigation. Arrow keys move, Enter opens, Esc closes.
Debounce the search 200ms.

### Other keyboard shortcuts

`/` focus search · `g` then `l` library · `g` then `h` home · `?` shortcut sheet ·
`Esc` close any overlay. In the reader: `←`/`→` pages, `f` fullscreen, `b` bookmark.

---

## Pages

### Public

**`/` Home** — Hero with the command palette teaser. "Trending searches" row.
Curated shelves as horizontal scrollers (Featured, New this week, Free to read).
Category grid.

**`/books` Catalogue** — The core browsing surface. Left sidebar with facet filters
(category, author, language, format, price range, minimum rating). Grid/list toggle.
Sort dropdown. **Infinite scroll using the cursor from the API** — never page
numbers. Filters live in the URL query string so a filtered view is shareable and
survives a refresh.

**`/books/:slug` Book detail** — Large cover, title, authors, rating. Sticky buy
panel on desktop. Tabbed: Description / Details / Reviews. "Related books" carousel.
The primary button is state-dependent: **Buy · Read now · In your library**.

**`/authors/:slug`, `/categories/:slug`** — Listing pages, same grid component.

**`/search`** — Full search results with facets, matching the catalogue layout, plus
the query echoed and a result count phrased as "about N results" (the API's total is
an estimate, never exact).

### Auth

**`/login`, `/register`, `/forgot-password`, `/reset-password`, `/verify-email`** —
Centred cards, minimal. Real-time password strength on register. OAuth buttons
(Google, GitHub) that redirect to the API's OAuth start URL.

**Two-step login:** `POST /v1/auth/login` may return `{ mfa_required: true,
challenge_token, expires_in }` instead of tokens. In that case show a 6-digit code
input and submit to `POST /v1/auth/login/mfa` with the challenge token. Support a
"use a recovery code instead" link — the same endpoint accepts either.

### Authenticated

**`/library`** — Owned books. Grid with a reading-progress ring on each cover.
Filter: All / In progress / Finished / Downloaded.

**`/read/:bookId`** — The reader. This is the app's centrepiece.
- Distraction-free: chrome fades out after 3s of no mouse movement, returns on move.
- Controls: font size, line height, font family (serif/sans), theme (light / sepia /
  dark), margin width.
- Progress bar at the bottom; "12 min left in this chapter".
- Bookmarks and highlights. `b` toggles a bookmark at the current position.
- Progress saves automatically — **debounce to at most one write every 10s**, and
  flush on unmount and on `visibilitychange`.

**`/wishlist`, `/collections`, `/collections/:id`** — Saved books. Collections are
user-created named lists; support drag to reorder.

**`/orders`, `/orders/:id`** — Order history and detail with a download-invoice link.

**`/checkout`** — Cart review → billing details → payment. See the payment flow below.

**`/account`** — Tabbed: Profile · Security (password, 2FA setup with a QR code,
active sessions with a "revoke" button per session) · Notifications (preference
toggles) · Connected accounts.

**`/notifications`** — The full list behind the bell icon.

### Stub these (the API is not built yet)

- **AI assistant** — Design the UI (a chat drawer, "Ask about this book") but wire it
  to mock responses only, behind a `VITE_FEATURE_AI=false` flag.
- **Admin dashboard** — Do not build it at all. Skip entirely.

---

## API contracts

Base URL: `VITE_API_BASE_URL`. Every path below is relative to it.

### Conventions that apply everywhere

**Errors.** Every failure returns this shape, with the HTTP status carrying the
meaning:

```json
{ "error": { "code": "book_not_purchasable", "message": "Human readable.",
             "details": {}, "request_id": "..." } }
```

Show `message` to the user. Never show `code` or `request_id` in the UI — put
`request_id` in a copy-to-clipboard affordance on the error page only.

**Money is always an integer of the currency's minor unit** — `price_minor: 49900`
means ₹499.00. Never do float arithmetic on it. Format with
`Intl.NumberFormat(locale, { style: 'currency', currency }).format(minor / 100)`.
A helper `formatMoney(minor, currency)` should be the only place that division
appears.

**Pagination is cursor-based** on the catalogue, search, orders and notifications:

```json
{ "items": [...], "next_cursor": "eyJ2Ijoi...", "has_more": true }
```

Pass `?cursor=<next_cursor>&limit=20`. When `next_cursor` is null there are no more
pages. **There is no total and no page number** — build infinite scroll, not a pager.
(Taxonomy lists — authors, publishers, categories — use `?page=1&limit=20` and return
`{ items, meta: { page, limit, total, pages } }`.)

**Dates** are ISO 8601 UTC strings.

### Authentication

```
POST /v1/auth/register     { email, password, full_name? }
POST /v1/auth/login        { email, password, device_label? }
POST /v1/auth/login/mfa    { challenge_token, code }
POST /v1/auth/refresh      {}          ← send credentials: 'include'
POST /v1/auth/logout       {}
POST /v1/auth/logout/all   {}
GET  /v1/auth/me
PATCH /v1/auth/me          { full_name?, avatar_url?, locale? }
POST /v1/auth/change-password    { current_password, new_password }
POST /v1/auth/forgot-password    { email }
POST /v1/auth/reset-password     { token, new_password }
POST /v1/auth/verify-email       { token }
POST /v1/auth/verify-email/resend
GET  /v1/auth/sessions
DELETE /v1/auth/sessions/{id}
GET  /v1/auth/mfa/status
POST /v1/auth/mfa/enroll        → returns a TOTP secret + otpauth URI for a QR code
POST /v1/auth/mfa/confirm       { code }
POST /v1/auth/mfa/disable       { password }
POST /v1/auth/mfa/recovery-codes
GET  /v1/auth/oauth/providers
GET  /v1/auth/oauth/{provider}/start    ← redirect the browser here
GET  /v1/auth/oauth/accounts
DELETE /v1/auth/oauth/accounts/{provider}
```

Login/refresh return:

```json
{ "access_token": "eyJ...", "token_type": "Bearer", "expires_in": 900,
  "csrf_token": "...", "user": { "id", "email", "full_name", "avatar_url",
  "roles": [], "permissions": [], "email_verified", "mfa_enabled", "is_active" } }
```

**Token handling — get this right, it is the part that is painful to retrofit:**

- The **access token lives in memory only** (a React context / module variable).
  Never `localStorage`, never a cookie you set. It expires in 15 minutes.
- The **refresh token is an httpOnly cookie** the API sets. Your code cannot read it
  and must not try. Every request to `/v1/auth/refresh` and `/v1/auth/logout` needs
  `credentials: 'include'`.
- Send the access token as `Authorization: Bearer <token>` on every other request.
- On a **401**, call refresh **once**, then retry the original request. If refresh
  also fails, clear state and route to `/login`.
- **Queue concurrent 401s.** If five requests fail at once, refresh once and replay
  all five — do not fire five refreshes. This matters: the API rotates refresh tokens
  and treats a replayed one as a compromise, which logs the user out everywhere.
- Send the `csrf_token` from the login response back as an `X-CSRF-Token` header when
  calling refresh.
- Refresh **proactively** at ~80% of `expires_in` so requests rarely see a 401 at all.

### Catalogue

```
GET /v1/books?q=&category=&author=&publisher=&language=&format=
             &min_price_minor=&max_price_minor=&free_only=
             &sort=&cursor=&limit=&include_facets=true
GET /v1/books/{slug}
GET /v1/books/{slug}/related
GET /v1/books/{bookId}/access          ← may this user read it?
GET /v1/books/{bookId}/download?format=pdf
GET /v1/authors?page=&limit=&q=        · GET /v1/authors/{slug}
GET /v1/categories?page=&limit=        · GET /v1/categories/{slug}
GET /v1/categories/tree                ← whole hierarchy in one response
GET /v1/publishers?page=&limit=        · GET /v1/publishers/{slug}
```

A book in a list looks like:

```json
{ "id", "slug", "title", "subtitle", "status", "language",
  "price_minor": 49900, "discount_price_minor": null, "effective_price_minor": 49900,
  "currency": "INR", "cover_key", "thumbnail_key",
  "available_formats": ["pdf","epub"], "rating_average": 4.3, "rating_count": 128,
  "view_count", "published_at",
  "authors": [{ "id", "name", "slug", "avatar_key" }],
  "categories": [{ "id", "name", "slug", "icon" }] }
```

**Always display `effective_price_minor`.** When `discount_price_minor` is set, show
`price_minor` struck through beside it.

**`cover_key` and `thumbnail_key` are storage keys, not URLs.** Build the URL as
`${VITE_CDN_BASE_URL}/${cover_key}`. Put that in one helper — `coverUrl(key, size)` —
and use a neutral placeholder when the key is null.

**`/download` returns a short-lived signed URL**, not the file. Fetch it, then set
`window.location.href` to the returned `url`. If it returns **402**, the user does
not own the book — show the buy panel. **403** means their access is read-only.

### Library, reading, engagement

```
GET  /v1/library?cursor=&limit=           · GET /v1/library/detailed
GET  /v1/reading-progress
GET  /v1/reading-progress/{bookId}
PUT  /v1/reading-progress/{bookId}    { position, percent, location? }
GET,POST /v1/bookmarks                · PATCH,DELETE /v1/bookmarks/{id}
GET,POST /v1/wishlist                 · DELETE /v1/wishlist/{bookId}
GET,POST /v1/collections              · GET,PATCH,DELETE /v1/collections/{id}
POST /v1/collections/{id}/items       · DELETE /v1/collections/{id}/items/{bookId}
GET,POST /v1/books/{bookId}/reviews
PATCH,DELETE /v1/reviews/{id}         · POST /v1/reviews/{id}/vote
```

A library item is `{ book: BookListItem, source, granted_at, expires_at,
can_download, progress_percent }`.

A review has a `status` of `pending | approved | rejected | flagged`. Show a
"Awaiting moderation" note on the author's own pending review; hide other people's
non-approved reviews entirely.

### Search

```
GET /v1/search?q=&filter=category:fiction&filter=author:jane-doe
              &sort=&min_price_minor=&max_price_minor=&min_rating=
              &free_only=&limit=&cursor=&facets=true
GET /v1/search/suggest?q=&limit=8
GET /v1/search/trending?limit=10
GET /v1/search/related/{bookId}
POST /v1/search/click   { query_id, book_id, position }
```

**Filters are repeatable `name:value` query parameters** — `?filter=category:fiction
&filter=category:history` means "fiction OR history". Different names AND together.
Allowed names: `category`, `author`, `language`, `format`, `tag`, `publisher`.

The search response carries a **`query_id`**. When a user clicks a result, POST it to
`/v1/search/click` with the zero-based position. Fire and forget — never block
navigation on it.

`/suggest` powers the command palette. Results come back interleaved by kind
(`book` | `author` | `category` | `query`), each with `text`, `href` and an optional
`subtitle`. **Render them in the order given** — do not re-sort by score.

`estimated_total` is an estimate. Render "about 1,240 results", never "1,240 results".

### Checkout and payments

```
GET  /v1/payments/providers
POST /v1/checkout/quote      { items: [{ book_id, quantity }], coupon_code?, billing? }
POST /v1/coupons/validate    { code, items: [...] }
POST /v1/orders              { items, coupon_code?, provider?, billing?, return_url? }
GET  /v1/orders?cursor=&limit=       · GET /v1/orders/{id}
POST /v1/orders/{id}/cancel  { reason? }
POST /v1/payments/verify     { order_id, provider, provider_order_id,
                               provider_payment_id, signature }
GET  /v1/invoices            · GET /v1/invoices/{id}
GET  /v1/plans               · GET,POST /v1/subscriptions
POST /v1/subscriptions/{id}/cancel   { at_period_end }
```

**The cart is client-side only** — persist it in `localStorage`. There is no cart
API. The cart holds book ids and quantities; it never holds prices.

**Never compute a total in the frontend.** Call `/v1/checkout/quote` and display what
it returns. It is the only source of the subtotal, discount, tax and total. Re-quote
whenever the cart or the coupon changes (debounce the coupon field 400ms).

The quote returns:

```json
{ "lines": [{ "book_id", "title", "quantity", "unit_price_minor",
              "line_total_minor", "already_owned": false }],
  "subtotal_minor", "discount_minor", "taxable_minor",
  "tax": { "percent": 18, "cgst_minor", "sgst_minor", "igst_minor",
           "total_minor", "place_of_supply" },
  "total_minor", "currency", "coupon_applied": true, "coupon_message": null }
```

Show the GST breakdown in the order summary: **CGST + SGST as two lines** when both
are non-zero, **IGST as one line** otherwise. Prices already include tax, so the
total equals the sum of the listed prices minus any discount — do not add tax on top.

Mark any line with `already_owned: true` clearly ("Already in your library") and let
the user remove it.

**A bad coupon returns HTTP 200** with `coupon_applied: false` and a readable
`coupon_message`. Show that message inline under the field — it is not an error
state, so no red banner and no toast.

**The checkout flow:**

1. `GET /v1/payments/providers` → which gateways exist, plus the *public* keys.
2. `POST /v1/orders` with an **`Idempotency-Key: <uuid>` header** — generate one UUID
   per checkout attempt and reuse it across retries. This is what makes a
   double-clicked Buy button safe.
3. The response is a checkout session: `{ order, provider, provider_order_id,
   publishable_key, checkout_url, client_secret, amount_minor, currency }`.
4. **Razorpay** → open Razorpay Checkout with `publishable_key` and
   `provider_order_id`. On success it hands you `razorpay_payment_id`,
   `razorpay_order_id` and `razorpay_signature` — POST all three to
   `/v1/payments/verify`.
   **Stripe** → redirect to `checkout_url`.
5. **`amount_minor: 0` means the order settled instantly** (free book, or a 100%
   coupon). There is no gateway step — go straight to the success page.
6. On the success page, **poll `GET /v1/orders/{id}` until `status` is `paid`** (every
   2s, give up after 30s and show "we'll email you when it completes"). The webhook
   may land before or after the redirect; do not assume either.

Order statuses: `pending | awaiting_payment | paid | failed | cancelled | refunded |
partially_refunded`.

### Notifications

```
GET  /v1/notifications?cursor=&limit=&unread_only=
GET  /v1/notifications/unread-count
POST /v1/notifications/read              { notification_ids?: [] }
POST /v1/notifications/{id}/archive
GET,PUT /v1/notifications/preferences
POST /v1/notifications/unsubscribe       { token, category? }
GET  /v1/notifications/channels
```

The bell polls `/unread-count` every 60s — **not the full list**. Fetch the list only
when the dropdown opens.

Preferences come back with a `locked: true` flag on transactional categories
(receipts, password resets). **Render those toggles disabled but visible**, with a
tooltip explaining they are essential account messages. Do not hide them.

`/unsubscribe` is reached from an email link at `/unsubscribe?token=...` and works
**without being signed in** — build that page as a public route.

---

## Non-negotiables

1. **No backend.** No Supabase, no database, no server functions. Only HTTP calls to
   `VITE_API_BASE_URL`.
2. **One API layer.** `src/lib/api/` with a client per domain (`auth.ts`, `books.ts`,
   `search.ts`, `payments.ts`, `notifications.ts`), each exporting typed functions.
   Mock and real adapters share one interface.
3. **Types mirror the API.** Put them in `src/lib/api/types.ts`. Money fields end in
   `_minor` and are `number`. Ids are `string` (UUIDs).
4. **Access token in memory only.** Never localStorage.
5. **Never compute prices client-side.** Quote endpoint only.
6. **Cursor pagination, not page numbers**, everywhere the API uses cursors.
7. **Accessible.** Semantic HTML, labelled inputs, focus traps in modals, `aria-live`
   on toasts, WCAG AA contrast in both themes.
8. **Responsive from 360px up.** The reader and the catalogue must both work on a
   phone.

## Environment variables

```
VITE_API_BASE_URL=https://gateway-production-c3e0.up.railway.app
VITE_CDN_BASE_URL=
VITE_USE_MOCK_API=true
VITE_FEATURE_AI=false
```

`VITE_CDN_BASE_URL` is intentionally empty for now — object storage does not have a
public domain yet. **`coverUrl(key, size)` must handle that**: when the base URL is
empty or the key is null, return a generated placeholder (a gradient derived from the
book id, with the title's initials) rather than a broken image. Covers are the single
most visible element of this app, so the fallback needs to look deliberate, not like
a failure. When the CDN domain lands, setting one variable fixes every image.

Commit a `.env.example` with these and read them through one `src/lib/config.ts` —
never `import.meta.env` scattered through components.

Start with the design system, the API layer with mocks, and the catalogue. Then the
book detail page, auth, the library and the reader, then checkout.

═══════════════════ COPY TO HERE ═══════════════════

---

## After Lovable is done

Three things make the handover a config change instead of a rewrite. Check them
before you accept the result:

1. **`grep -r "supabase" src/` returns nothing.** If Lovable enabled Supabase
   anyway, tell it to remove it before you go further — it gets harder later.
2. **`grep -rn "fetch(" src/components/` returns nothing.** All network calls belong
   in `src/lib/api/`.
3. **`VITE_USE_MOCK_API=false` is the only switch.** If flipping it requires code
   changes, the adapters have drifted apart.

### Then, to integrate

```bash
# 1. Flip off the mocks — the URL is already correct
VITE_USE_MOCK_API=false

# 2. Allow the frontend origin on every service
CORS_ORIGINS=https://<your-frontend-domain>

# 3. Allow the checkout return URL on the payment service
ALLOWED_RETURN_ORIGINS=https://<your-frontend-domain>
```

Everything the frontend calls goes through the **gateway**, not the individual
services. The gateway already routes `/v1/auth`, `/v1/books`, `/v1/search`,
`/v1/orders`, `/v1/checkout`, `/v1/notifications` and the rest to the right place.

### One tradeoff worth knowing

Lovable builds a **Vite SPA**, not Next.js. For an e-book store that means **book
pages are not server-rendered**, so Google sees an empty shell on first crawl. For a
storefront that lives on organic search for book titles, that is a real cost.

Two ways to handle it, in order of effort:

- **Accept it now, migrate later.** The API layer this prompt specifies ports to
  Next.js almost unchanged — the components and the client are the same, only the
  routing and data fetching move. This is the pragmatic path if you want something
  live quickly.
- **Prerender the important routes.** `vite-plugin-ssr` or a prerender step for
  `/books/:slug` gets you most of the SEO benefit without leaving the Lovable stack.

Either way, have Lovable set proper `<title>`, `<meta name="description">` and
Open Graph tags per route with `react-helmet-async` — that alone covers link previews
in WhatsApp, Slack and Twitter, which is where a lot of book sharing actually happens.
