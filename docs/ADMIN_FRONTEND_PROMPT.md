# Admin console — build prompt

A paste-ready brief for building the **KnowledgeOS operator console**. Unlike the
customer app (see `FRONTEND_LOVABLE_PROMPT.md`, which is now an integration spec),
this one does not exist yet.

**Everything below is generated from the running services**: 77 admin endpoints across
nine services, with their real permission requirements.

Read [Before you paste](#before-you-paste) first — there are two facts about roles that
will otherwise cost you a rebuild.

---

═══════════════════ COPY FROM HERE ═══════════════════

Build the **KnowledgeOS admin console** — the internal operator tool for an e-book
store. Customers never see this. Its users are three or four people who live in it all
day.

## The single most important instruction

**Do not create a backend. Do not enable Supabase, a database, auth, storage or edge
functions.**

A complete REST API exists and is deployed. Every piece of data comes from it over
HTTP through one base URL:

```
VITE_API_BASE_URL = https://api.allelearning.in
```

That is the API gateway; it routes every path below to the right service. **Never call
a service directly.** `GET /health` checks it; `/docs` is the live OpenAPI and the
authoritative contract if anything here is ambiguous.

Build against mocks first so a backend hiccup never blocks UI work:

- Every network call behind a typed client in `src/lib/api/`.
- `src/lib/api/mock/` with realistic fixtures.
- `VITE_USE_MOCK_API` (default `true`) switches between them.
- **Mock and real adapters implement the same TypeScript interface**, so flipping the
  flag is the whole integration.

Never call `fetch` from a component. Components call hooks; hooks call the client.

## Stack

React + TypeScript + Tailwind + shadcn/ui. TanStack Query for server state, TanStack
Table for the data grids, React Router. Recharts for the two charts. No state library
beyond Query + context.

## The design bar

This is a **dense professional tool**, not a marketing site. Think Linear, Stripe
Dashboard, Vercel — not a SaaS landing page.

- **Information density over whitespace.** An operator scanning 200 orders wants 40
  rows on screen, not 8. Compact row heights, 13px table text, tabular numerals for
  anything numeric so columns align.
- **Light mode first** here, dark mode second — the inverse of the customer app.
  Operators use this next to spreadsheets and email in daylight.
- **No decorative animation.** Transitions only where they explain a state change.
- **Every destructive action gets a confirm dialog that names the thing.** "Refund
  ₹499.00 to arjun@example.com?" — not "Are you sure?"
- **Every table** has: loading skeletons, an empty state that says what would appear
  here, an error state with a copy-to-clipboard `request_id`, column sort where the
  API supports it, and URL-synced filters so a view can be shared in Slack.
- **Money is right-aligned, monospace, and always shows its currency.**
- Keyboard: `⌘K` command palette (jump to any page, search users/orders/books), `/`
  focus search, `Esc` close.

---

## Roles — read this before building navigation

The API enforces **permissions, not roles**, and there are two facts that will bite:

**1. `admin` is not the top role, and it cannot change settings.** The `admin` role
grants `books:*`, `users:read`, `users:write`, `orders:read`, `orders:refund`,
`reviews:moderate`, `automation:*`, `analytics:read` and `ai:use`. It does **not**
grant `settings:write`.

So an `admin` cannot manage coupons, plans, notification templates, feature flags, or
trigger a scheduled job. Only `superadmin` can. That is deliberate — those are the
highest-blast-radius actions on the platform — but it means **a console that shows
every nav item to an `admin` will 403 on roughly a third of them.**

**2. Drive navigation off `permissions`, never off `roles`.** `GET /v1/auth/me`
returns the caller's resolved `permissions` array. Hide any nav item whose permission
the user lacks. A disabled-looking button that 403s is worse than an absent one.

| Section | Permission required |
|---|---|
| Dashboard, Health, Search analytics, AI spend, Notification stats | `analytics:read` |
| Books, Entitlements | `books:write` (delete needs `books:delete`, publish needs `books:publish`) |
| Moderation queue | `reviews:moderate` |
| Users, Audit log | `users:read` (ban/unban needs `users:write`) |
| Orders, Invoices | `orders:read` (refund needs `orders:refund`) |
| Coupons, Plans, Templates, Flags, Worker triggers | `settings:write` — **superadmin only** |
| Automation pipeline | `automation:read` (run/retry needs `automation:run`) |

## Pages

### `/` — Dashboard
One call: `GET /v1/admin/dashboard?days=7`. Returns `panels[]`, each with its **own
status** (`ok` / `error` / `timeout` / `unavailable`) and its own `data` object.

**Render each panel independently.** A panel with `status !== "ok"` shows an
unavailable card with its `error` next to the panels that worked — never a whole-page
error. `complete: false` is normal and not an alarm.

Window switcher (7/30/90 days) and a refresh button that passes `?refresh=true`.

### `/health` — Service health
`GET /v1/admin/health-board`. One row per service: name, status, version, latency.
Green/amber/red. Poll every 15s while the tab is focused, never when hidden.

### `/users` — Directory
`GET /v1/admin/users` — offset paginated **with a real total**, so this gets a proper
pager, not infinite scroll. Search box (substring on email and name), status filter
(`active` / `banned` / `unverified` / `deleted`), sortable by `created_at`,
`last_login_at`, `email`. Stat cards above from `GET /v1/admin/users/stats`.

Row → `/users/:id` drawer: profile, roles, `banned_at` / `ban_reason`, MFA state, and
**Ban / Unban** buttons behind `users:write`.

Ban opens a dialog that **requires a reason** (min 3 chars — the API rejects a blank
one). Warn in the dialog that this signs them out everywhere.

### `/audit` — Audit log
`GET /v1/admin/audit-logs?days=30`. Filters: action (populate the dropdown from
`GET /v1/admin/audit-logs/actions` — never hard-code it), user, actor, window. Show
`meta` as expandable JSON.

### `/moderation` — Review queue
`GET /v1/admin/moderation/reviews` — **oldest first**, and keep it that way; the top of
the queue is the customer who has waited longest. Tabs for pending/approved/rejected/
flagged with counts from `GET /v1/admin/moderation/reviews/counts`.

Each row: rating, title, body, book, verified-purchase badge. Approve / Reject via
`POST /v1/reviews/{review_id}/moderate`. Optimistically remove the row and refetch the counts.

### `/books` — Catalogue
`GET /v1/admin/books` — cursor paginated (infinite scroll, no total), status filter
including drafts. Create, edit, publish, unpublish, soft-delete. Upload via
`POST /v1/admin/books/uploads` → presigned POST → then `POST /v1/automation/jobs` with
the returned key to start processing.

Grant/revoke access manually at `/books/:id/entitlements`.

### `/orders` — Orders and refunds
`GET /v1/admin/orders` with status and date filters. Detail shows line items, the tax
breakdown, payment attempts and refunds.

**Refund** (`orders:refund`) takes an amount in **minor units** and a reason. The
dialog must show the formatted amount and the customer's email, and must not allow
more than the refundable balance. Partial refunds are allowed.

`mark-paid` exists for payments settled outside the platform — put it behind an extra
confirm; it is the one action here that fabricates money in the ledger.

### `/coupons`, `/plans` — Commerce config *(superadmin)*
Standard CRUD. Coupon percent vs fixed, usage limits, validity window. Plan prices are
minor units.

### `/automation` — Ingestion pipeline
`GET /v1/automation/jobs` with status filter, and `GET /v1/automation/stats`.

Job detail is the important screen: **the 13-stage history**, each stage with its
status (`pending` / `running` / `succeeded` / `failed` / `skipped`), duration, output
and error. Render it as a vertical timeline. A skipped stage is **grey, not red** — it
means "correctly decided there was nothing to do", and colouring it as a failure
trains people to ignore red.

Retry (with optional `from_stage`), cancel, and run-now. Bulk import at
`/automation/imports` — the form must default `dry_run` to **true** and show the
per-row error table with line numbers before offering a real run.

### `/search` — Index health
`GET /v1/admin/search/health`, `/analytics`, `/runs`. Reindex button
(`POST /v1/admin/search/reindex`, `mode: reconcile | full`) with a confirm — `full`
empties the index first. Zero-result queries table is the useful one: it is a list of
things customers wanted and could not find.

### `/ai` — Spend
`GET /v1/admin/ai/usage`, `/spend`, `/failures`. Gauge of `today_cost_usd` against
`daily_limit_usd`. Line chart from `/spend`. Show cached/blocked/failed as **separate**
figures — never folded into the total. Cost per generation divides by
`billable_requests`, not `total_requests`.

### `/notifications` — Delivery
Templates CRUD with **preview before save** (`POST .../preview` renders without
sending and names missing variables). Deliveries table with status and error.
Suppression list with add/remove — removal is deliberate, so confirm it.

### `/workers` — Scheduler
`GET /v1/admin/workers/jobs` — the schedule with each job's last outcome and
consecutive failures. `GET /v1/admin/workers/health` — **`stale_jobs` is the field to
surface most prominently**; a scheduler that has stopped produces no errors, only
absence. Run-now per job (`settings:write`). Run history at `/workers/runs`.

`skipped_locked` is a **normal** outcome meaning another replica took the run — style
it neutrally, not as a failure.

### `/flags` — Feature flags *(superadmin)*
List, create, edit, delete. Rollout percentage slider, allowlist of user ids, and the
change history at `/flags/:key/audit` showing **before and after** for each change.

Editing requires a `reason` field — it lands on the audit row.

---

## API conventions

**Errors:**
```json
{ "error": { "code": "...", "message": "Human readable.", "details": {}, "request_id": "01J..." } }
```
Show `message`. Put `request_id` behind a copy button on the error state.

**Auth** is identical to the customer app: `POST /v1/auth/login` returns
`{ access_token, expires_in, csrf_token, user }` or
`{ mfa_required: true, challenge_token }`. Keep the access token **in memory**; refresh
via `POST /v1/auth/refresh` with `credentials: 'include'` and the `X-CSRF-Token`
header. Refresh **rotates** the token, so single-flight your 401 retry queue or two
parallel refreshes will log the operator out.

**Require MFA for this app.** If `GET /v1/auth/mfa/status` says it is off, show a
blocking prompt to enrol. An operator console without a second factor is one phished
password away from the whole platform.

**Money is an integer of the currency's minor unit.** `49900` is ₹499.00. One
`formatMoney(minor, currency)` helper; no float arithmetic anywhere.

**Two pagination styles, and they are not interchangeable:**

| Style | Shape | Where |
|---|---|---|
| Offset **with total** | `{ items, total, limit, offset }` | users, audit logs, moderation, automation jobs/imports, worker runs, flags audit |
| Cursor, **no total** | `{ items, next_cursor, has_more }` | admin books, orders, notification deliveries |

Give the first a real pager with page numbers. Give the second infinite scroll — there
is no total and asking for one is a full table scan.

**Dates** are ISO 8601 UTC. Render relative ("4 min ago") with the absolute timestamp
in a tooltip.

---

## Endpoint reference

### Dashboard & platform — `admin`
- `GET /v1/admin/dashboard` · `GET /v1/admin/health-board`
- `GET /v1/admin/flags` · `POST` · `GET|PATCH|DELETE /v1/admin/flags/{key}` · `GET /v1/admin/flags/{key}/audit`

### Users & audit — `auth`
- `GET /v1/admin/users` · `GET /v1/admin/users/stats` · `GET /v1/admin/users/{user_id}`
- `POST /v1/admin/users/{user_id}/ban` · `POST /v1/admin/users/{user_id}/unban`
- `GET /v1/admin/audit-logs` · `GET /v1/admin/audit-logs/actions`

### Catalogue & moderation — `books`
- `GET|POST /v1/admin/books` · `POST /v1/admin/books/uploads`
- `GET|PATCH|DELETE /v1/admin/books/{book_id}`
- `POST /v1/admin/books/{book_id}/publish` · `/unpublish` · `/versions`
- `POST /v1/admin/entitlements` · `DELETE /v1/admin/entitlements/{entitlement_id}`
- `GET /v1/admin/moderation/reviews` · `GET /v1/admin/moderation/reviews/counts`
- `POST /v1/reviews/{review_id}/moderate`

### Commerce — `payment`
- `GET /v1/admin/orders` · `GET /v1/admin/orders/{order_id}`
- `POST /v1/admin/orders/{order_id}/refund` · `/mark-paid` · `GET .../refunds`
- `GET /v1/admin/invoices` · `GET /v1/admin/revenue`
- `GET|POST /v1/admin/coupons` · `PATCH|DELETE /v1/admin/coupons/{coupon_id}`
- `GET|POST /v1/admin/plans` · `PATCH /v1/admin/plans/{plan_id}`
- `GET /v1/admin/webhooks` · `POST /v1/admin/webhooks/{webhook_event_id}/replay`

### Pipeline — `automation`
- `GET|POST /v1/automation/jobs` · `GET /v1/automation/jobs/{job_id}`
- `POST /v1/automation/jobs/{job_id}/retry` · `/cancel` · `/run`
- `GET|POST /v1/automation/imports` · `GET /v1/automation/imports/{import_id}`
- `GET /v1/automation/stats`

### Search — `search`
- `GET /v1/admin/search/health` · `/analytics` · `/runs`
- `POST /v1/admin/search/reindex` · `DELETE /v1/admin/search/trending`

### AI — `ai`
- `GET /v1/admin/ai/usage` · `/spend` · `/failures`

### Notifications — `notifications`
- `GET /v1/admin/notifications/stats` · `/deliveries`
- `GET|POST /v1/admin/notifications/templates`
- `PATCH|DELETE /v1/admin/notifications/templates/{template_id}`
- `POST /v1/admin/notifications/templates/{template_id}/preview`
- `GET|POST|DELETE /v1/admin/notifications/suppressions`

### Scheduler — `workers`
- `GET /v1/admin/workers/jobs` · `GET /v1/admin/workers/jobs/{job_name}`
- `POST /v1/admin/workers/jobs/{job_name}/run`
- `GET /v1/admin/workers/runs` · `GET /v1/admin/workers/health`

---

## Non-negotiables

1. **No backend, no database, no Supabase.**
2. **Navigation is driven by `permissions` from `/v1/auth/me`**, never by role name.
3. **Money never touches a float.**
4. **Every destructive action names its target in the confirm dialog.**
5. **Skipped ≠ failed.** Pipeline skips and `skipped_locked` worker runs are neutral.
6. **`request_id` on every error state**, behind a copy button.
7. **The dashboard renders partial results.** One dead service is one grey card.

═══════════════════ COPY TO HERE ═══════════════════

---

## Before you paste

**The `admin` role cannot write settings.** Covered above, but worth repeating here
because it is the one thing that will produce a console that looks finished and 403s
in a demo. Either accept it and gate those sections on `settings:write`, or add
`Permission.SETTINGS_WRITE` to `UserRole.ADMIN` in
`packages/core-py/knowledgeos_core/schemas.py` and redeploy — a one-line change, and a
real decision about who can change pricing.

**Give the console its own origin and add it to CORS.** Every service reads
`CORS_ORIGINS`; the admin app is a second origin, so it must be listed too:

```
CORS_ORIGINS=https://yourfrontend.app,https://admin.yourfrontend.app
```

**Consider not exposing this publicly at all.** The endpoints are permission-checked,
but an operator console reachable from the internet is a credential-stuffing target
with a very high payoff. A Railway private network, a VPN, or Cloudflare Access in
front of it costs nothing and removes the entire class of problem.
