# Notification service

Email, SMS, WhatsApp, push and in-app messages — templates, per-category
preferences, delivery tracking, retries and suppression.

- **Port** 8006 · **Schema** `notifications` · **28 endpoints** · **69 tests**

---

## It is driven by events, not by callers

Almost nothing calls this service directly. It **consumes platform events**: a user
registers, an order is paid, a refund is issued — and this service decides those
facts deserve a message.

That indirection is the point. The auth service should not know what a welcome email
says, and the payment service should not know that a receipt exists. Otherwise every
service grows its own copy of the platform's tone of voice, and changing a subject
line becomes a five-service deploy.

The event → template mapping is one table in `services/events.py`. A handler per
event would put the logic in ten places; a table can be read at a glance.

When a service does need something specific, it publishes `notification.requested`
with a template key — or calls `POST /internal/send`. Either way it names a
**template**, never a body.

---

## The rule that outranks everything

**An address on the suppression list is never contacted again.** Not for a receipt,
not for a password reset, not for anything.

That is not an oversight. Continuing to mail an address that hard-bounced or issued
a spam complaint costs the sending domain its reputation — and that takes every other
message down with it, password resets included. One angry customer who marked a
newsletter as spam is not worth losing the ability to deliver to an entire mailbox
provider.

Removal is an operator action, never automatic. A bounce that resolves itself on
retry is exactly the pattern that gets a domain blocklisted.

Precedence, in order:

1. **Suppression** — beats everything.
2. **Transactional categories** — cannot be opted out of. A receipt is not marketing.
3. **The user's stored preference.**
4. **The default**, which is on.

All four are evaluated in one place (`PreferenceService.may_send`). A send path that
checked preferences in one branch and suppression in another would eventually grow a
third branch that checked neither.

---

## Sending

One path — `Dispatcher.send` — used by the internal API, the event consumers and the
retry sweep alike. The order is the design:

1. **Resolve the template.** Missing for one channel is normal (a push message has no
   HTML body); missing for *every* requested channel is a 404, because a silent
   success means a receipt nobody notices is absent.
2. **Render.** Missing variables **abort the send**. A customer receiving
   `Hi {{first_name}},` is an apology; a 422 is a bug report with the names attached.
3. **Ask permission.** A refusal is recorded as a `skipped` delivery *with its
   reason*, so "why did they not get their receipt?" is answerable from the database
   rather than from logs.
4. **Send, record, and schedule a retry only when retrying could help.**

**In-app is written first**, because it is a database row rather than a network call
and therefore the channel most likely to land. A message visible in the app while the
email is still retrying is the right way round.

### Retries

Exponential backoff from `RETRY_BASE_SECONDS`, driven by the worker rather than
inline — retrying inside the request that failed would make a customer wait out a
backoff before their page loads.

A **permanent** failure is not retried at all. The receiving server already said the
mailbox does not exist; asking again several times an hour looks like a dictionary
attack. Permanent email failures suppress the address; permanent push failures
deactivate the token, because pushing to uninstalled apps gets a sender throttled by
FCM.

The rendered body is **stored on the delivery row**, not a preview. A retry has to
send the same message the first attempt did.

---

## Templates

Stored, not hard-coded — the most common reason to change a notification is a typo or
a tone problem, and neither should need a release.

Rendering is `{{name}}` substitution **and nothing else**. No Jinja, no expression
evaluation, no attribute access. Templates are editable through the admin API, so a
template language with arbitrary evaluation would be remote code execution behind an
admin token; server-side template injection is a well-trodden path from "content
editor" to "shell".

Values substituted into the **HTML** body are escaped; values in the text body are
not. Escaping the text body would show a customer `&amp;` in their own name.
A substituted value containing `{{x}}` is never re-substituted, so a display name
cannot be used to read another variable.

`POST /templates/{id}/preview` renders without sending and names any missing
variables. Every copy change should go through it.

Locale falls back to English. A customer would rather read a receipt in the wrong
language than never receive one.

---

## Preferences and unsubscribe

There is **no preference row per user per category at signup** — writing millions of
rows that all say "yes" makes the table useless. Absence means the default.

Transactional categories come back with `locked: true`. The UI should render those
toggles disabled rather than hiding them: a user is entitled to see that the message
exists and that it is not marketing. An attempt to disable one is accepted and
ignored rather than rejected — a 400 for a state the user cannot reach is noise.

Unsubscribe links carry a **signed token**, never a user id. A URL containing an id
lets anyone unsubscribe anyone by editing it. Emails also carry `List-Unsubscribe`
and `List-Unsubscribe-Post` (RFC 8058) — Gmail and Yahoo require them on bulk mail,
and a working one-click unsubscribe is far better for deliverability than the
alternative the user reaches for otherwise.

---

## Endpoints

### User
`/v1/notifications` (list, keyset paginated) · `/unread-count` · `/read` ·
`/{id}/archive` · `/preferences` (GET, PUT) · `/unsubscribe` · `/devices` ·
`/channels`

The unread count is its own endpoint because the bell polls it far more often than it
fetches the list.

### Admin (`settings:write` / `analytics:read`)
Templates (list, create, update, deactivate, preview), deliveries, delivery stats and
the suppression list.

### Internal (HMAC-signed)
`POST /internal/send` — how every other service asks for a message. Plus the worker's
retry and pruning sweeps.

---

## Running it

```bash
cp .env.example .env
alembic upgrade head
python -m main                # http://localhost:8006/docs
```

Tests need no infrastructure and no mail server:

```bash
PYTHONPATH=. pytest tests/ -q
```

The email provider in tests is a **recording** stub implementing the real
`ChannelProvider` protocol, and it can be told to fail transiently or permanently —
which is what makes the retry and suppression paths testable rather than theoretical.

---

## Things worth knowing before you change this

**`use_enum_values` bites here too.** `SendRequest.channels` arrives as plain strings
when the caller sends it and as enum members when it defaults, so `channel is
NotificationChannel.EMAIL` silently never matches — which looks exactly like "email is
disabled". Channels are coerced back to enum members at the top of `send()` so the
rest of the code can rely on identity.

**Delivery rows store the full rendered body.** Storing a truncated preview and
retrying from it would quietly mail customers half a message — a bug that only
surfaces when a provider has a bad afternoon.

**`SENT` and `DELIVERED` are different statuses.** A provider accepting a message says
nothing about whether a mailbox took it, and conflating them makes a deliverability
problem invisible.

**The bounce rate excludes skipped deliveries.** Including them would understate it
exactly when a suppression list is growing.
