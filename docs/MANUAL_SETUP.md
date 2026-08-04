# Manual Setup

**Everything on this page requires you.** These are credentials, keys and third-party
accounts that cannot be generated or guessed — each one is a placeholder in
`.env.example` waiting for a real value.

Work top to bottom. Sections are ordered by when you actually need them.

> **The platform runs locally with none of this.** Payments, AI, SMS and OAuth all
> degrade gracefully when unconfigured, so you can develop the catalogue, reader and
> checkout UI without a single external account. Come back here when you need a
> specific capability, or when you are preparing to deploy.

**Legend**
🔴 **Required to deploy** · 🟡 **Required for that feature** · 🟢 **Optional**

---

## 1. 🔴 JWT signing keys

The auth service signs access tokens with an RSA private key; every other service
verifies with the public key. Without these, nobody can log in.

```bash
./scripts/generate-keys.sh
```

That prints two PEM blocks. Set them as:

| Variable | Value | Where |
|---|---|---|
| `JWT_PRIVATE_KEY` | the private PEM | **auth service only** |
| `JWT_PUBLIC_KEY` | the public PEM | auth service |

**Pasting into Railway:** the dashboard mangles multi-line values. Convert newlines
to a literal `\n` first — the script prints a ready-to-paste single-line form.

**Never put the private key on any service other than `auth`.** It is the one secret
that, if leaked, lets an attacker mint an admin token for the entire platform.

Rotation: generate a new keypair, add it to the auth service's key table alongside
the current one, let it become the active signer. Old tokens keep verifying against
the old public key until they expire. No coordinated deploy required.

---

## 2. 🔴 Internal API secret

Signs service-to-service calls. Private-network reachability is not authorisation.

```bash
openssl rand -hex 32
```

Set `INTERNAL_API_SECRET` to that value on **every** service — they must match. On
Railway use a shared variable so rotating it updates all services at once.

---

## 3. 🔴 Database and Redis

On Railway, add the **Postgres** and **Redis** plugins to the project, then reference
them rather than copying values:

```
DATABASE_URL=${{Postgres.DATABASE_URL}}
REDIS_URL=${{Redis.REDIS_URL}}
```

Railway hands out `postgres://…`; the platform rewrites the scheme to
`postgresql+asyncpg://` automatically, so paste it unchanged.

Locally, `pnpm stack:up` provides both with the defaults already in `.env.example`.

---

## 4. 🔴 Object storage (MinIO or S3)

Book files, covers and generated assets.

**On Railway** — deploy MinIO from its public image with a volume at `/data`:

```
S3_ENDPOINT_URL=http://minio.railway.internal:9000     # internal, for signing
S3_PUBLIC_ENDPOINT_URL=https://storage.yourdomain.com  # what the browser resolves
S3_ACCESS_KEY=<MINIO_ROOT_USER>
S3_SECRET_KEY=<MINIO_ROOT_PASSWORD>                    # openssl rand -hex 24
S3_BUCKET=knowledgeos
S3_USE_SSL=true
```

The two endpoint variables genuinely differ: the API signs URLs using the internal
hostname, then rewrites the host to the public one before handing the URL to a
browser. Setting them to the same private value produces links that work in tests and
fail for every real user.

**Using AWS S3 / Cloudflare R2 / Backblaze B2 instead:** set `S3_ENDPOINT_URL` to the
provider's endpoint and supply that provider's access key pair. The bucket needs a
`public/` prefix readable by anyone and everything else private.

---

## 5. 🔴 Meilisearch

```bash
openssl rand -base64 32   # must be at least 16 bytes
```

```
MEILISEARCH_URL=http://meilisearch.railway.internal:7700
MEILISEARCH_MASTER_KEY=<generated>
```

Deploy from `getmeili/meilisearch:v1.12` with a volume at `/meili_data`. The index is
rebuildable from Postgres, so losing this volume is recoverable — run the reindex
script rather than restoring a backup.

---

## 6. 🟡 OAuth — Google

Needed only for "Sign in with Google".

1. <https://console.cloud.google.com/> → create or select a project
2. **APIs & Services → OAuth consent screen** → External → fill in app name, support
   email, and the scopes `email`, `profile`, `openid`
3. **Credentials → Create Credentials → OAuth client ID → Web application**
4. **Authorised redirect URIs** — add every environment you will use:
   ```
   http://localhost:8000/v1/auth/oauth/google/callback
   https://api.yourdomain.com/v1/auth/oauth/google/callback
   ```
5. Copy the client id and secret:
   ```
   GOOGLE_CLIENT_ID=…apps.googleusercontent.com
   GOOGLE_CLIENT_SECRET=…
   ```

The redirect URI must match **byte for byte**, including the trailing-slash state and
the scheme. A mismatch produces `redirect_uri_mismatch`, and it is almost always this.

Publishing the consent screen out of "Testing" mode is required before users outside
your test list can sign in.

## 7. 🟡 OAuth — GitHub

1. <https://github.com/settings/developers> → **New OAuth App**
2. Authorisation callback URL:
   ```
   http://localhost:8000/v1/auth/oauth/github/callback
   ```
3. Generate a client secret and copy both values:
   ```
   GITHUB_CLIENT_ID=…
   GITHUB_CLIENT_SECRET=…
   ```

GitHub allows only **one** callback URL per app, so create a separate OAuth app per
environment.

---

## 8. 🟡 Payments — Razorpay (India)

1. <https://dashboard.razorpay.com/> → **Settings → API Keys → Generate**
2. ```
   RAZORPAY_KEY_ID=rzp_test_…      # rzp_live_… in production
   RAZORPAY_KEY_SECRET=…
   ```
3. **Settings → Webhooks → Add New Webhook**
   - URL: `https://api.yourdomain.com/v1/webhooks/razorpay`
   - Events: `payment.captured`, `payment.failed`, `order.paid`, `refund.processed`,
     `subscription.charged`, `subscription.cancelled`
   - Copy the webhook secret:
     ```
     RAZORPAY_WEBHOOK_SECRET=…
     ```

**The webhook secret is not optional, and it is a different value from
`RAZORPAY_KEY_SECRET`.** They sign different things: the webhook secret signs the raw
request body, the API secret signs the `<order_id>|<payment_id>` callback from the
Checkout modal. Using one where the other belongs fails closed — the service refuses
the callback rather than trusting it — which is the right direction to fail but is
confusing if you do not know to look for it.

Without a webhook secret the service cannot prove a callback came from Razorpay, so
it refuses **every** webhook. Orders will be created and never settle.

Local testing needs a public URL — use `ngrok http 8000` and register the ngrok URL
as a temporary webhook endpoint.

## 9. 🟡 Payments — Stripe (international)

1. <https://dashboard.stripe.com/apikeys>
   ```
   STRIPE_SECRET_KEY=sk_test_…
   STRIPE_PUBLISHABLE_KEY=pk_test_…
   ```
2. **Developers → Webhooks → Add endpoint**
   - URL: `https://api.yourdomain.com/v1/webhooks/stripe`
   - Events: `checkout.session.completed`, `payment_intent.succeeded`,
     `payment_intent.payment_failed`, `charge.refunded`,
     `customer.subscription.updated`, `customer.subscription.deleted`
   - ```
     STRIPE_WEBHOOK_SECRET=whsec_…
     ```

Local testing: `stripe listen --forward-to localhost:8000/v1/webhooks/stripe`
prints a `whsec_…` for the CLI session — use that one locally.

### GST (India)

Four settings on the payment service, and every one of them has legal weight rather
than being a technical default. Set them deliberately.

```
GST_PERCENT=18            # current rate on digital goods — verify against current law
SELLER_STATE_CODE=GJ      # your GST registration state
PRICES_INCLUDE_TAX=true   # catalogue prices already contain GST
SELLER_GSTIN=…            # required on every invoice if you are registered
SELLER_LEGAL_NAME=…       # the registered entity name, not the brand
INVOICE_PREFIX=KOS        # appears in every invoice number
```

**`SELLER_STATE_CODE` decides which government gets paid.** A sale to a buyer in the
same state splits into CGST + SGST; a sale to another state is a single IGST line.
The customer is charged the same either way, so getting this wrong is invisible in
the checkout flow and surfaces at filing time.

**`PRICES_INCLUDE_TAX=true`** (the Indian retail norm) means a book listed at ₹499
charges ₹499, with the GST extracted from it. Setting it to `false` adds 18% at
checkout instead — legal in some contexts, but be certain that is what you intend,
because it changes what every existing price means.

Invoice numbers are issued as a consecutive serial within the financial year
(April–March): `KOS/2026-27/000001`. That format is a legal requirement, and gaps in
the sequence are a question an auditor will ask — do not delete invoice rows.

---

## 10. 🟡 AI providers

At least one is needed for the automation pipeline's description, summary and tagging
stages. Without any, those stages are skipped and books ingest with metadata only.

| Provider | Get a key | Variable |
|---|---|---|
| Anthropic *(default)* | <https://console.anthropic.com/settings/keys> | `ANTHROPIC_API_KEY=sk-ant-…` |
| OpenAI | <https://platform.openai.com/api-keys> | `OPENAI_API_KEY=sk-…` |
| Google | <https://aistudio.google.com/apikey> | `GOOGLE_AI_API_KEY=…` |
| OpenRouter | <https://openrouter.ai/keys> | `OPENROUTER_API_KEY=sk-or-…` |
| Ollama *(local, free)* | `ollama serve` | `OLLAMA_BASE_URL=http://localhost:11434` |

```
AI_DEFAULT_PROVIDER=anthropic
AI_DEFAULT_MODEL=claude-sonnet-5
AI_MONTHLY_BUDGET_USD=100
```

**Set the budget.** The pipeline pauses AI stages when the month's spend crosses it,
rather than quietly running up a bill. A bulk import of 5,000 books is thousands of
LLM calls, and this is the guardrail between a normal invoice and a memorable one.

For development, Ollama with a small local model costs nothing and exercises the same
code path.

---

## 11. 🟡 Email

**Development:** nothing to configure. Mailpit catches everything at
<http://localhost:8025> — no message can escape to a real inbox.

**Production** — any SMTP provider works. Resend, Postmark and SES are all fine:

```
SMTP_HOST=smtp.resend.com
SMTP_PORT=587
SMTP_USER=resend
SMTP_PASSWORD=re_…
SMTP_FROM_EMAIL=noreply@yourdomain.com
SMTP_FROM_NAME=KnowledgeOS
SMTP_USE_TLS=true
```

You must also configure **SPF, DKIM and DMARC** on the sending domain, or verification
and receipt emails land in spam. Every provider documents the exact DNS records; this
is a real deliverability requirement, not a nicety.

## 12. 🟢 SMS and WhatsApp

```
TWILIO_ACCOUNT_SID=AC…          # https://console.twilio.com/
TWILIO_AUTH_TOKEN=…
TWILIO_PHONE_NUMBER=+1…

WHATSAPP_PHONE_NUMBER_ID=…      # https://developers.facebook.com/ → WhatsApp
WHATSAPP_ACCESS_TOKEN=…
```

WhatsApp requires **pre-approved message templates** for anything outside a 24-hour
customer-initiated window. Submit templates for order confirmation and delivery
before launch; approval takes days, not minutes.

## 13. 🟢 Push notifications

Firebase Cloud Messaging:

1. <https://console.firebase.google.com/> → create a project
2. **Project settings → Service accounts → Generate new private key**
3. Minify the downloaded JSON to a single line:
   ```bash
   jq -c . service-account.json
   ```
   ```
   FCM_CREDENTIALS_JSON={"type":"service_account",…}
   ```

---

## 14. 🔴 Domains and public URLs

```
NEXT_PUBLIC_SITE_URL=https://yourdomain.com
NEXT_PUBLIC_API_URL=https://api.yourdomain.com
CORS_ORIGINS=https://yourdomain.com
TRUSTED_HOSTS=yourdomain.com,api.yourdomain.com
```

Two things that are security-relevant, not cosmetic:

- **`CORS_ORIGINS` must never be `*` in production.** With
  `cors_allow_credentials=True` a wildcard origin lets any site on the internet make
  authenticated requests on your users' behalf.
- **Nothing secret may go in a `NEXT_PUBLIC_*` variable.** They are inlined into the
  JavaScript bundle and readable by anyone who opens devtools.

In Railway, add the custom domain to the `frontend` and `gateway` services and point
your DNS at the CNAME each one provides.

---

## 15. 🟢 Error tracking

```
SENTRY_DSN=https://…@….ingest.sentry.io/…
```

---

## Deployment checklist

Before the first production deploy:

- [ ] `JWT_PRIVATE_KEY` set on `auth` **only**; `JWT_PUBLIC_KEY` where needed
- [ ] `INTERNAL_API_SECRET` identical across every service, and not the dev default
- [ ] `ENVIRONMENT=production` on every service (this closes `/docs` and enables HSTS)
- [ ] `CORS_ORIGINS` restricted to your real domains — **not** `*`
- [ ] `TRUSTED_HOSTS` set to your real hostnames
- [ ] Every payment webhook secret configured, and a test event delivered successfully
- [ ] `S3_PUBLIC_ENDPOINT_URL` set to the browser-reachable host, not the internal one
- [ ] `AI_MONTHLY_BUDGET_USD` set to a number you are willing to be billed
- [ ] SPF, DKIM and DMARC configured on the sending domain
- [ ] Postgres backups enabled in Railway
- [ ] Every default password from `.env.example` replaced
- [ ] `LOG_FORMAT=json` (structured logs are what make an incident debuggable)

## Rotating a secret

1. Generate the new value.
2. Update it in Railway (shared variables update every service at once).
3. Redeploy the affected services.
4. Revoke the old value at the provider.

For `INTERNAL_API_SECRET`, expect brief internal 403s during the rollout window while
services hold different values. Deploy during low traffic, or add the new secret as an
accepted alternate first if you need a zero-downtime rotation.
