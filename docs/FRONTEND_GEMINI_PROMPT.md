# Frontend completion — Gemini Flash prompt

A paste-ready brief for finishing the **KnowledgeOS customer app and operator
console** against the live API.

This is not a from-scratch brief. A React app already exists at `~/front-ebook`
with every route scaffolded and every network call already behind a typed client —
but it has **never run against the real backend**, because until now there wasn't
one. `VITE_USE_MOCK_API` defaults to `true` and every screen you have seen is
fixtures.

The backend is now deployed and verified. The job is to turn the mocks off and make
the app work against reality.

Read [Before you paste](#before-you-paste) first. There are four facts about the API
that will otherwise cost you a rebuild.

---

## Before you paste

**1. The API is live. Verify against it, do not guess.**
`https://gateway-production-c3e0.up.railway.app/openapi.json` is the authoritative
contract — 185 paths, 220 operations, 256 schemas — and it is generated from the
running services. `docs/API.md` is the prose version. When this prompt and the spec
disagree, the spec is right.

**2. Two services are deliberately absent.** The automation service is not deployed;
every `/v1/automation/*` call returns 502. Razorpay and Stripe have no credentials;
`GET /v1/payments/providers` returns `{"providers": [], "default": null}`. Both are
scope decisions, not outages. The app must degrade cleanly, not show a broken page.

**3. The existing code is good. Do not rewrite it.** In-memory access tokens, a
refresh queue that collapses parallel 401s into one refresh, proactive refresh at 80%
of `expires_in`, CSRF handling, mock and real adapters behind one interface. That is
the hard part and it is done. You are finishing, not restarting.

**4. Money is an integer.** Every price on the wire is `*_minor` — paise, not rupees.
`49900` is ₹499.00. Never parse it into a float. Format at the render boundary only.

---

═══════════════════ COPY FROM HERE ═══════════════════

You are completing an existing React + TypeScript e-book store — a customer-facing
shop and an operator console — against a REST API that is already deployed and
working.

## The single most important instruction

**Do not create a backend. Do not add a database, auth provider, ORM, storage
bucket, or serverless function.**

A complete REST API exists. Every piece of data comes from it over HTTP through one
base URL:

```
VITE_API_BASE_URL = https://gateway-production-c3e0.up.railway.app
```

That is the gateway; it routes every path to the right service. **Never call a
service directly.** `GET /health` checks it. `GET /openapi.json` is the live
specification and the authoritative contract whenever anything here is ambiguous.

## Where the code is

`~/front-ebook` — Vite + React 19 + TypeScript + Tailwind v4 + TanStack Query +
React Router 7. Already present:

- `src/lib/api/client.ts` — fetch wrapper, in-memory token, refresh queue, CSRF
- `src/lib/api/{auth,books,library,payments,search,notifications,ai,admin}.ts` —
  each exports a mock adapter and a real adapter behind one shared interface
- `src/lib/api/mock/` — fixtures
- `src/context/{Auth,Cart,Theme}Context.tsx`
- Customer pages: Home, Catalogue, BookDetail, AuthorDetail, CategoryDetail, Search,
  Library, Reader, Wishlist, Collections, Orders, Checkout, Account, Notifications
- Auth pages: Login, Register, ForgotPassword, Unsubscribe
- Admin pages: Dashboard, Health, Users, AuditLogs, Moderation, Books, Orders,
  CommerceConfig, Search, AI, Notifications, Workers, Flags, Automation

Keep this structure. Components call hooks; hooks call the client. **Never call
`fetch` from a component.**

## Your task, in order

### Phase 1 — Make it real

Set `VITE_USE_MOCK_API=false` and work through every screen against the live API.
Keep the mock adapters working — they are how the UI stays buildable when the
network is not — but the real adapter is now the one that must be correct.

Expect drift. The mocks were written against a specification; the API is what
shipped. Fix the **adapter**, never the component, and never the mock in a way that
makes it disagree with the real shape.

Verify each screen loads real data before moving on. A screen that renders fixtures
is not done.

### Phase 2 — Close the gaps this list names

These are real differences between what the app assumes and what the API does. Each
one is confirmed against the deployed system.

**Catalogue writes live under `/v1/admin/`.** `POST /v1/books` does not exist — it is
`POST /v1/admin/books`. `/v1/books` is read-only and public. Taxonomy
(`/v1/authors`, `/v1/categories`, `/v1/publishers`) accepts writes at its own path
but still requires a staff token.

**Book creation payload.** `price_minor`, not `price`. `author_ids` is a list of
objects, not a list of ids:

```json
{"title": "…", "slug": "…", "price_minor": 49900, "currency": "INR",
 "language": "en", "formats": ["epub"],
 "author_ids": [{"author_id": "<uuid>", "role": "author"}],
 "category_ids": ["<uuid>"]}
```

**Book detail is by slug, access and download are by id.** `GET /v1/books/{slug}`
returns the book; `GET /v1/books/{book_id}/access` and
`GET /v1/books/{book_id}/download` take the UUID. Do not send a slug to either.

**The access response field is `has_access`, not `can_read`.**

```json
{"book_id": "…", "has_access": true, "can_download": true,
 "source": "purchase", "expires_at": null}
```

`can_download` is separately false for a subscription entitlement, so a Read button
and a Download button ask different questions of the same response.

**`POST /v1/orders` returns a `CheckoutSession`, not an order.** The order id is
`response.order.id`. `checkout_url` and `client_secret` are `null` here because no
gateway is configured, and `provider` is `"manual"`.

**Listed prices are GST-inclusive.** In a quote, `total_minor` equals
`subtotal_minor` — ₹499.00 is what the customer pays, of which ₹76.12 is tax.
**Never add `tax.total_minor` to `total_minor`**; that charges the tax twice. Show
`total_minor` as the price and the `tax` object as a breakdown underneath. Each
quote line also carries `already_owned`, which is how you stop someone re-buying a
book they have.

**Download takes an optional `?format=`.** Omit it and the book's preferred
available format is served. Only send one when the user picked it from
`available_formats`.

**Publishing requires an uploaded file.** The sequence is: create (draft) →
`POST /v1/admin/books/uploads` for a presigned target → `POST` the file to that URL
as multipart form data with the returned `fields` → record it with
`POST /v1/admin/books/{book_id}/versions` (`{"epub_key": "<key>", …}`) → then
`POST /v1/admin/books/{book_id}/publish`. Publishing earlier is a `409`:

```
409  {"error": {"code": "conflict",
                "message": "This book has no uploaded file, so it cannot be published."}}
```

Build the admin book form as that wizard. A publish button that 409s is a worse
experience than one that is disabled with the reason shown.

**Bytes never pass through the API.** Upload straight to the presigned URL. Show real
progress from the XHR, not a fake spinner.

**Autocomplete is `/v1/search/suggest`**, not `/v1/search/autocomplete`.

**Admin health is `/v1/admin/health-board`**, not `/v1/admin/health`.

**`GET /v1/admin/invoices` requires a `user_id` query parameter.** Omitting it is a
`422`. Reach it from a customer's detail page, not from a global nav item.

**Entitlement is eventually consistent.** Settlement publishes an event; the books
service consumes it and writes the entitlement. There is a **one-to-two second gap**
between an order being paid and `/v1/books/{id}/access` reporting `can_read: true`.
After checkout, poll `access` (or invalidate the query on an interval) for up to ~15
seconds before showing "something went wrong". Do not assume it is immediate and do
not give up after one try.

**Refresh needs CSRF.** The refresh token is an httpOnly cookie, and
`POST /v1/auth/refresh` requires the CSRF token alongside it. Without it:

```
401  {"error": {"code": "csrf_missing", "message": "A CSRF token is required to refresh."}}
```

`client.ts` already handles this — do not "simplify" it away.

**A reused refresh token signs the user out everywhere.** That is theft detection
working. Handle a 401 from `/refresh` by clearing state and routing to login. Never
retry it.

**Rate limits are tight on credentials.** Login is 10 per 15 minutes, registration 5
per hour, password reset 5 per hour — per IP when signed out. Render `429` as "too
many attempts, try again shortly", read `error.details.scope` to say which action,
and never auto-retry a credential call.

### Phase 3 — Handle what is not configured

Call these on load and let the answers drive the UI. Do not hardcode any of it.

**`GET /v1/payments/providers`** currently returns:

```json
{"providers": [], "default": null, "currency": "INR",
 "razorpay_key_id": null, "stripe_publishable_key": null}
```

An empty `providers` means no card checkout exists. Checkout must still work up to
order creation — quote, coupon, GST, order — and then show an honest "payment is not
enabled yet" state with the order saved and reachable from Orders. Do not render a
Razorpay or Stripe button. When credentials are added later, the same screen must
light up from this response with no code change.

Orders are settled by an operator at `POST /v1/admin/orders/{order_id}/mark-paid`.
The admin Orders page needs that action; it is currently the only way a purchase
completes.

**`GET /v1/notifications/channels`** currently returns:

```json
{"channels": ["in_app", "email"], "sending_enabled": true, "email_provider": "smtp"}
```

Render preference toggles **only for the channels in that array**. SMS, WhatsApp and
push are not configured; offering those switches promises something that cannot be
delivered.

Be aware that **email is queued but does not currently reach Gmail** — the sending
domain has no SPF or DKIM records. Do not build a flow whose only success path is
"check your inbox". Verification and password reset must both work from a link an
operator can retrieve, and the in-app inbox is the channel that actually delivers
today.

**`GET /v1/ai/status`** reports whether AI is usable *right now*. There is a hard
daily cost ceiling ($10 platform-wide, $1 per user) and reaching it returns `503`.
Call `status` before rendering any AI affordance. A chat button that 503s is worse
than one that was never drawn. The AI drawer already exists — gate it on this.

**`/v1/automation/*` returns 502.** The Admin → Automation page must render an
explicit "this service is not deployed" state. Not a spinner, not an error toast, and
not a blank table.

**The admin dashboard returns per-panel status.** Render each panel's own state:

```json
{"generated_at": "…", "window_days": 7,
 "panels": [{"service": "books", "status": "ok", "data": {…}},
            {"service": "automation", "status": "unavailable", "data": null}]}
```

One service being down is one grey card, never a red page. `automation` will always
be `unavailable` here — that is correct, and the card should say so plainly.

**`GET /v1/plans` is empty** until an operator creates plans at
`/v1/admin/plans`. The subscriptions area should show an empty state, not a skeleton
that never resolves. The same is true of coupons.

### Phase 4 — Errors, permissions, states

**Every error has the same shape.** Branch on `error.code`, show `error.message`, and
put `error.request_id` somewhere copyable in the dev build.

```json
{"error": {"code": "payment_required", "message": "…", "details": {…}, "request_id": "…"}}
```

Codes that need distinct UI: `payment_required` (402 — offer to buy, this is the
paywall), `forbidden` (403 — no amount of money helps, hide the action instead),
`rate_limited` (429), `service_unavailable` (503 — degraded, say which feature),
`validation_error` (422 — map `details.fields` onto the form fields),
`upstream_error` (502).

**402 and 403 are not the same.** 402 means "buy this" and should render the purchase
path. 403 means "not for you" and the affordance should not have been visible.

**Drive permissions from `GET /v1/auth/permissions`.** It returns what each role may
do. Hide actions the signed-in role cannot perform rather than letting them 403.
Roles are `user`, `moderator`, `admin`, `superadmin`.

**Every list needs four states**: loading (skeleton matching the final layout, not a
spinner), empty (say what would fill it and how), error (the message plus a retry),
and loaded. `src/components/ui/ListStates.tsx` exists — use it everywhere.

**Pagination comes in two shapes.** Cursor (`{items, next_cursor, has_more}`) for the
catalogue, orders and notifications — treat the cursor as opaque and stop on
`has_more: false`. Offset (`{items, total}`) for categories, authors and publishers.
Do not build one component that pretends they are the same.

### Phase 5 — Finish the customer journey

Make this path work end to end, against the live API, and demonstrate it:

browse catalogue → open a book → add to wishlist → checkout quote (GST shown
correctly) → create order → see it in Orders → *(operator marks it paid)* → the book
appears in Library → open the Reader → reading position syncs → invoice available.

`PUT /v1/reading-progress/{book_id}` takes `{"position": "epubcfi(…)", "percent":
12.5}`. It is a `402` without an entitlement — the Reader must not be reachable for a
book the user does not own.

## Stack rules

- Keep the current stack. Do not add a state management library — TanStack Query is
  the server state, React context is the little client state that remains.
- Tailwind v4. No CSS-in-JS.
- Dark and light must both work; `ThemeContext` exists.
- Mobile first. The catalogue and the Reader are the two screens people actually use
  on a phone.
- Keyboard: the command palette and shortcut sheet already exist. Keep them working.

## Quality bar

- `pnpm build` passes with no TypeScript errors. `pnpm lint` clean.
- No `any` in `src/lib/api/`. The adapters are the type boundary; if a response is
  hard to type, generate the type from `/openapi.json` rather than widening it.
- No secret in any `VITE_*` variable — those are inlined into the browser bundle.
- No access token in `localStorage` or a non-httpOnly cookie. It lives in memory;
  that is already how `client.ts` works.
- Every screen tested against the live API before you call it done.

## What "done" means

`VITE_USE_MOCK_API=false`, and a person can sign in, browse, wishlist, order, and —
after an operator settles the order — read the book. An operator can sign in, create
a book with a real uploaded file, publish it, see it in the catalogue, find the
order, mark it paid, and watch the dashboard reflect all of it.

Every unconfigured capability says so in plain language on the screen where it would
have appeared.

═══════════════════ COPY TO HERE ═══════════════════

---

## Notes for whoever runs this

**Credentials.** A superadmin exists. Do not put its password in the repo, in a
`VITE_*` variable, or in this file — type it into the login form. If it is lost, use
the password reset flow; email now works.

**Verifying without a card.** There is no payment gateway, so the only way to
complete a purchase is `POST /v1/admin/orders/{order_id}/mark-paid` as an operator.
That path exercises everything a real capture would: settlement, the event bus, the
entitlement grant, the invoice. Build against it.

**Two accounts, two browsers.** The operator flow and the customer flow need
different roles, and the app holds one token in memory. Use two profiles rather than
signing in and out.

**When the API and this document disagree, the API is right.** Everything here was
true when written and was read off the running system; the spec at `/openapi.json`
regenerates itself.
