# Payment service

Orders, checkout, Razorpay and Stripe gateways, refunds, coupons, GST invoicing,
subscriptions and affiliate commission. The only service on the platform allowed to
decide that money moved.

It does **not** grant access to anything. When an order settles it publishes
`payment.succeeded` and `order.paid`; the books service consumes those and writes the
entitlement. That split is deliberate — a payment service that also handed out files
would need to know about formats, storage keys and reader permissions, and a bug in
either half would compromise both.

- **Port** 8005 · **Schema** `payment` · **42 endpoints** · **154 tests**

---

## The four rules

Everything in this service follows from these. If you change one, change it
knowingly.

**1. Money is an integer of the currency's minor unit.** Paise, cents. There is no
float and no `Decimal` anywhere — not in a column, not on the wire, not in a
calculation. Floats cannot represent 0.1, and a float price column produces invoices
that do not reconcile.

**2. The client never sends a price.** `POST /v1/orders` carries book ids and
quantities. The amount is resolved from the catalogue over a signed internal call.
Any endpoint that accepted a client-supplied amount would let someone buy a book for
one paisa, and no amount of downstream validation makes that safe again.

**3. Confirmations arrive more than once.** Providers redeliver webhooks, the browser
posts its own verification, operators replay stored events. Every path into
settlement is idempotent, three times over (see below).

**4. Nothing is hard-deleted.** A cancelled order stays. A refund is a new row plus a
status change. A coupon is deactivated, never removed. This is financial data — the
history *is* the record.

---

## Settlement, and why it cannot double-grant

`PaymentService.settle()` is reachable from three places: the provider webhook, the
client-side `/v1/payments/verify` call, and an operator's replay. Any of them can run
twice. Three independent mechanisms stop that mattering:

1. **`payments` is unique on `(provider, provider_payment_id)`.** The database refuses
   to record one capture twice.
2. **`OrderService.mark_paid()` returns `False` if the order was already settled.**
   The side effects — invoice, affiliate credit, events — run only on `True`.
3. **Every downstream effect is itself idempotent.** The invoice is looked up before
   it is issued; the affiliate conversion is unique per order; the entitlement grant
   on the books side is unique per `(user, book, source, order)`.

Any one would usually do. Together they mean no single mistake double-grants a book
or double-issues an invoice.

**Events are published after the transaction commits**, never inside it. Publishing
inside would announce a payment that then rolled back — and a book granted for money
that was never taken is far worse than a book granted a second late.

---

## Webhooks

The endpoints are unauthenticated by design: a provider cannot hold one of our access
tokens. **The signature is the authentication.**

The handlers read the **raw request body**, not a parsed model. Re-serialising JSON
changes bytes — key order, whitespace, unicode escaping — and a signature is over
bytes. This is why there is no Pydantic request model on these two routes.

Order of operations, which is not negotiable:

1. Verify the signature over the raw body.
2. Record the event — **verified or not**. An unverified delivery is evidence; a burst
   of them is someone probing the endpoint.
3. Reject unverified events before any state changes.
4. Deduplicate on `(provider, event_id)`.
5. Process, and record the outcome on the event row.

**Always answer 2xx once the signature verifies**, even when processing failed. A 5xx
makes a provider retry with backoff for days, turning one broken handler into a
stampede. Failures are recorded on the `webhook_events` row and replayed deliberately
by an operator via `POST /v1/admin/webhooks/{id}/replay` — which only ever runs
against an event whose signature already verified, so a replay cannot launder an
unsigned payload into the system.

### The two signature schemes

| | Secret | Signed message |
|---|---|---|
| Razorpay webhook | `RAZORPAY_WEBHOOK_SECRET` | raw body |
| Razorpay checkout callback | `RAZORPAY_KEY_SECRET` | `<order_id>\|<payment_id>` |
| Stripe webhook | `STRIPE_WEBHOOK_SECRET` | `<timestamp>.<raw body>` |

Razorpay's two secrets are different values, and signing one check with the other is
an easy mistake — it fails closed, which is the right direction to fail.

Stripe puts the timestamp **inside** the signed message. An attacker who captures a
valid webhook cannot move it forward in time without invalidating the signature, so
rejecting old timestamps rejects replays. Several `v1` values may appear during a
secret rotation; any one matching is a pass.

---

## GST

Implemented in `services/tax.py` as pure integer functions.

- **Intra-state** (buyer's state == `SELLER_STATE_CODE`) → CGST + SGST, each half the
  rate. **Inter-state** → a single IGST line at the full rate. The total is identical
  either way; what differs is which government is paid, which is why getting it wrong
  is a compliance problem rather than a cosmetic one.
- **Buyer outside India** → export of services, no GST.
- **Rounding happens once, on the order total** — never per line. Rounding each line
  and summing produces a figure that disagrees with tax computed on the sum, and the
  invoice then does not add up.

`PRICES_INCLUDE_TAX=true` (the default, and the Indian retail norm) means the
catalogue price already contains GST and the tax is *extracted* from it. A page that
says ₹499 charges ₹499. Adding 18% on top at checkout is both a compliance issue and
the most reliable way to make a customer abandon a cart.

Invoice numbers are a **consecutive serial within the financial year** (April–March),
as the law requires — `KOS/2026-27/000001`. That is why they are not UUIDs: a gap in
the sequence is a question an auditor will ask. An invoice is immutable once issued;
a correction is a credit note, and there is no `update` method.

---

## Coupons

Validation is forgiving; redemption is strict.

A wrong code returns **200 with `valid: false`** and a sentence written for a customer
to read. A 4xx would make the checkout page render a failure banner for a typo.

Redemption is where a coupon becomes money, so the usage counter moves with a
**conditional UPDATE** (`WHERE usage_count < usage_limit`), not a read-modify-write.
Under a burst — a code posted to a large audience — read-modify-write lets a hundred
concurrent checkouts all read the same count and every one of them oversell the limit.

A coupon is consumed at **order creation**, not at payment. An abandoned checkout has
its coupon released by `expire_stale()`; the reverse mistake — consuming it only on
success — lets one single-use code fund unlimited concurrent checkouts.

The discount applies to the **eligible** subtotal. A coupon scoped to one category
must not discount the rest of the cart, and computing it against the whole subtotal is
the easy mistake that gives away far more than intended.

---

## Endpoints

### Checkout
| Method | Path | Notes |
|---|---|---|
| `GET` | `/v1/payments/providers` | Which gateways this deployment can use, plus public keys |
| `POST` | `/v1/checkout/quote` | Price a cart. Read-only, public |
| `POST` | `/v1/coupons/validate` | 200 + reason for a bad code |
| `POST` | `/v1/orders` | Create an order. Send `Idempotency-Key` |
| `GET` | `/v1/orders` | Your history, keyset paginated |
| `GET` | `/v1/orders/{id}` | 404 for someone else's |
| `POST` | `/v1/orders/{id}/cancel` | Unpaid orders only |
| `POST` | `/v1/payments/verify` | Client-side confirmation after the modal closes |

### Webhooks
`POST /v1/webhooks/razorpay` · `POST /v1/webhooks/stripe`

### Billing
`/v1/invoices` · `/v1/plans` · `/v1/subscriptions` · `/v1/affiliate`

### Admin
Orders, refunds, manual settlement, coupons, plans, webhook inspection and replay,
revenue. Guarded by `orders:read`, `orders:refund`, `settings:write` and
`analytics:read`.

### Internal (HMAC-signed)
Order lookup, purchased books, subscription status, and the three maintenance sweeps
the worker calls: expire orders, approve matured affiliate conversions, expire lapsed
subscriptions.

---

## Running it

```bash
cp .env.example .env          # then fill in the gateway credentials
alembic upgrade head
python -m main                # http://localhost:8005/docs
```

Tests need no infrastructure — in-memory SQLite plus `fakeredis`:

```bash
PYTHONPATH=. pytest tests/ -q
```

The catalogue is stubbed in the fixtures because pricing needs *some* source of book
prices. Signature verification, coupon arithmetic and the whole settlement path run
for real.

---

## Things worth knowing before you change this

**`use_enum_values` is a trap.** `BaseSchema` sets it, so enum fields on a request
model arrive as plain strings — *but only the ones the client actually sent*. A field
left at its enum default keeps the enum member. So `payload.provider.value` works
while the client omits the field and raises `AttributeError` the moment they send it.
Every enum on this platform is a `StrEnum`; use `str(field)`.

**Open a SAVEPOINT before you `session.add()`, not after.** `begin_nested()`
autoflushes pending state first, so a row added beforehand has its INSERT emitted
*outside* the savepoint — and the `IntegrityError` you were guarding against escapes
the `try` and takes the surrounding transaction with it. This service relies on
"insert and catch the unique violation" in four places and every one of them is inside
a transaction that is settling an order.

**Never assign `func.now()` to a Python attribute.** It is a SQL expression, so the
attribute is expired at flush and reading it back emits a lazy `SELECT` that asyncio
cannot serve inline — `MissingGreenlet`, at serialisation time, long after the line
that caused it. Use `datetime.now(UTC)`.

**A refund is attributable, bounded and gateway-idempotent.** `actor_id` records who
authorised it; the amount is checked against `refundable_minor` and backed by a
`refunded_minor <= total_minor` constraint; and the gateway is called with the refund
row's own id as its idempotency key, so a timeout-and-retry returns the original
refund rather than issuing a second one.
