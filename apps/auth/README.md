# Auth Service

Identity for KnowledgeOS. **This is the only process on the platform that can mint an
access token** — it holds the RSA private key; every other service verifies with the
public key from `/.well-known/jwks.json`.

Design rationale: [ADR 0003](../../docs/adr/0003-authentication.md).

- **Port** 8001 · **Schema** `auth` · **Depends on** PostgreSQL, Redis

## What it owns

Registration and login · email verification · password reset · refresh-token
rotation with reuse detection · OAuth (Google, GitHub) · TOTP two-factor and
recovery codes · sessions and device management · RBAC · audit logging · JWKS
publication and key rotation.

## Endpoints

| | |
|---|---|
| `POST /v1/auth/register` | Create an account (responds identically for duplicates) |
| `POST /v1/auth/login` | Sign in; returns tokens or an MFA challenge |
| `POST /v1/auth/login/mfa` | Complete a two-factor sign-in |
| `POST /v1/auth/refresh` | Rotate the refresh token |
| `POST /v1/auth/logout` · `/logout/all` | End this session · every session |
| `POST /v1/auth/verify-email` · `/verify-email/resend` | Email confirmation |
| `POST /v1/auth/forgot-password` · `/reset-password` · `/change-password` | Password flows |
| `GET` · `PATCH /v1/auth/me` | Profile |
| `GET /v1/auth/permissions` | Role → permission reference data |
| `GET` · `DELETE /v1/auth/sessions[/{id}]` | Device management |
| `POST /v1/auth/mfa/{enroll,confirm,disable,recovery-codes}` · `GET /mfa/status` | Two-factor |
| `GET /v1/auth/oauth/{providers,{provider}/start,{provider}/callback}` | OAuth |
| `GET` · `DELETE /v1/auth/oauth/accounts[/{provider}]` | Linked accounts |
| `GET /.well-known/jwks.json` | **Public signing keys** |
| `/internal/users/*` | Service-to-service (HMAC-signed) |

Full schemas: `/docs` when `ENVIRONMENT` is not `production`.

## The security properties, and what enforces them

These are not incidental — each has a test that fails if it regresses.

**Refresh rotation with reuse detection.** Every refresh consumes one token and
issues its successor, sharing a `family_id`. Presenting an already-consumed token
means two parties hold the chain, so the *entire family* is revoked. Without this, a
stolen refresh token is a silent 30-day credential.

**No user enumeration.** Registration, login and password reset return identical
responses whether or not the address exists — *and* take comparable time. An unknown
address is verified against a dummy bcrypt hash, because an early return that skips
the ~250ms verification is a working timing oracle on its own.

**Access token in memory, refresh token in an httpOnly cookie.** An XSS that reaches
`localStorage` would otherwise obtain a 30-day credential. The access token dies with
the tab.

**CSRF double-submit on refresh.** The browser sends the refresh cookie
automatically; a cross-site page cannot read the CSRF cookie to populate the matching
header, so it cannot forge a refresh.

**Token type confusion is blocked.** MFA challenges and access tokens are signed by
the same key, so the `typ` claim is checked. A challenge carries no roles or
permissions.

**TOTP replay is blocked.** The highest accepted time-step is recorded, so a code
observed over the shoulder cannot be reused inside its own 30-second window.

**Secrets at rest.** Passwords: bcrypt cost 12, pre-hashed above 72 bytes so long
passphrases keep their full entropy. Refresh, reset, verification and recovery
tokens: stored SHA-256 hashed. TOTP secrets: Fernet-encrypted, so a database dump
alone does not yield a working second factor.

**OAuth account linking requires a verified email.** Auto-merging on a matching
address is a known takeover route — an attacker registers the victim's address at a
provider that does not verify it and inherits the local account. Unverified means the
user must sign in with their password and link from settings.

**OAuth state is HMAC-signed and expiring**, and carries the PKCE verifier, so a
forged or replayed callback is rejected with no server-side session.

## Configuration

See [`.env.example`](.env.example) for every variable. The ones that matter:

| Variable | Notes |
|---|---|
| `JWT_PRIVATE_KEY` | **Required in production.** Set on this service only. |
| `JWT_PUBLIC_KEY` | Derived from the private key when omitted. |
| `JWT_PREVIOUS_PUBLIC_KEY` | Published alongside during a rotation. |
| `INTERNAL_API_SECRET` | Must match across every service. |
| `DATABASE_URL` · `REDIS_URL` | Railway reference variables. |
| `COOKIE_SECURE` | `true` everywhere except plain-HTTP local dev. |
| `GOOGLE_*` · `GITHUB_*` | OAuth stays disabled when unset. |

Generate a keypair:

```bash
./scripts/generate-keys.sh          # PEMs plus paste-ready escaped one-liners
./scripts/generate-keys.sh --env    # just the two env lines
```

Outside production, a throwaway keypair is generated at boot so the service starts
with no configuration. It logs a warning, and **in production it refuses to boot
without a key** — an ephemeral key would invalidate every session on every restart
and replicas would disagree with each other.

### Key rotation (no coordinated deploy)

1. Generate a new pair.
2. Move the current public key to `JWT_PREVIOUS_PUBLIC_KEY`.
3. Set the new pair and deploy. JWKS publishes both, so tokens signed before the swap
   keep verifying until they expire.
4. After one access-token lifetime, clear `JWT_PREVIOUS_PUBLIC_KEY`.

Verifiers select the key by the token's `kid`, and refresh their JWKS cache when they
meet an unknown one.

## Redis contract

Other services read these keys. **Changing the format is a breaking change.**

| Key | Value | TTL | Read by |
|---|---|---|---|
| `kos:denylist:user:{user_id}` | ban reason (bytes) | `ACCESS_TOKEN_TTL + 300s` | gateway |

Existence means banned. This is what makes a ban effective within seconds despite
access tokens staying cryptographically valid until they expire — the gateway checks
it on every request, so no per-request database read is reintroduced.

The TTL only has to outlive tokens minted before the ban: once those expire the user
cannot obtain new ones, because login and refresh both check the database.

## Events published

`user.registered` (carries the verification token for the notification service),
`user.verified`, `user.logged_in`, `user.password_reset_requested` (carries the reset
token).

## Running locally

```bash
cp .env.example .env
alembic upgrade head
python -m main                       # :8001
```

## Tests

```bash
PYTHONPATH=apps/auth pytest apps/auth/tests -q
```

76 tests, no infrastructure required — the suite runs on in-memory SQLite with
`fakeredis`, so the rate limiter, idempotency store and denylist all execute their
real code rather than being stubbed.

## Operational notes

- **Health**: `/health` is liveness (dependency-free). `/health/ready` reports
  Postgres and Redis. Railway's healthcheck points at `/health` deliberately —
  pointing it at readiness would turn a database blip into a restart loop.
- **Login is CPU-bound by design.** bcrypt at cost 12 is ~250ms. Scale on latency,
  and do not "optimise" it by lowering the cost factor.
- **Migrations run on deploy** via the `startCommand` in `railway.json`.
- Failed logins and lockouts appear as `auth.account_locked`; a spike in
  `knowledgeos_auth_events_total{outcome="failure"}` is the credential-stuffing
  signal, and there is a Prometheus alert for it.
