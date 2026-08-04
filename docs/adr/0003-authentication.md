# ADR 0003 — RS256 access tokens with a JWKS endpoint

**Status:** Accepted · **Date:** 2026-08-04

## Context

Nine services must authenticate requests. Three options:

1. **Session lookup** — every service calls auth on every request. Correct, revokable
   instantly, and it makes auth a hard dependency of every single request on the
   platform. One auth hiccup becomes a total outage.
2. **HS256 (shared secret)** — fast and offline, but *every service can mint tokens*.
   A vulnerability in the least-critical service yields admin tokens for the whole
   platform.
3. **RS256 (asymmetric)** — auth signs with a private key, everyone verifies with the
   public key.

## Decision

**RS256 access tokens, published via `/.well-known/jwks.json`.**

- The **auth service alone** holds the private key and is the only process that can
  mint a token.
- Every other service fetches the public key set, caches it for 10 minutes, and
  verifies **offline** — no network call on the request path.
- Tokens carry a `kid` header so keys can be rotated without a synchronised deploy.

### The token pair

| | Access token | Refresh token |
|---|---|---|
| Format | RS256 JWT | opaque, 32 random bytes |
| Lifetime | 15 minutes | 30 days |
| Storage | memory (frontend) | httpOnly, Secure, SameSite=Strict cookie |
| Verification | offline, by signature | database lookup |
| Revocable | no — expiry only | yes, immediately |

A JWT cannot be un-issued, so the access token's lifetime *is* the revocation
window. Fifteen minutes is short enough that a stolen token has limited value and
long enough that refreshes are not a hot path.

Refresh tokens are **opaque, not JWTs**, precisely because they must be revocable.
They are stored hashed (SHA-256) — a leaked database dump must not yield usable
tokens.

### Refresh rotation with reuse detection

Every refresh issues a new refresh token and invalidates the old one. If a token that
has already been used is presented again, that means it was stolen — the legitimate
client and the attacker cannot both hold the current one. The response is to
**revoke the entire token family**, forcing re-authentication.

This is the only mechanism that detects theft of a long-lived credential from a
client we do not control.

### What goes in the token

```jsonc
{
  "sub": "018f...",        // user id
  "email": "a@b.com",
  "roles": ["user"],
  "permissions": ["books:read", "ai:use"],
  "sid": "018f...",        // session id — lets one device be revoked
  "jti": "018f...",        // token id, for audit correlation
  "typ": "access",         // rejected if a refresh token is presented as access
  "iss": "knowledgeos-auth",
  "aud": "knowledgeos-api",
  "exp": 1234567890
}
```

Permissions are **embedded** rather than looked up, which is what keeps verification
offline. The tradeoff is staleness: a permission revoked mid-session stays effective
for up to 15 minutes. For the one case where that is unacceptable — a banned user —
the gateway checks a Redis denylist keyed by user id, so a ban takes effect within
seconds without reintroducing a per-request database read.

## Attack surface addressed explicitly

| Attack | Mitigation |
|---|---|
| `alg: none` / algorithm confusion | Header `alg` is checked against an allowlist **before** key material is loaded |
| Token from another system | `iss` and `aud` both verified |
| Refresh token used as access token | `typ` claim checked |
| Stolen refresh token | Rotation + reuse detection revokes the family |
| Key compromise | `kid`-based rotation, no coordinated deploy needed |
| Unknown `kid` flood → JWKS stampede | Forced refresh is rate-limited to one fetch per 10s |
| XSS stealing tokens | Access token in memory, refresh in `httpOnly` cookie |
| CSRF on refresh | `SameSite=Strict` + a double-submit CSRF token |
| Brute force | 10 attempts / 15 min per identifier, sliding window |
| Password DB leak | bcrypt cost 12, per-password salt, >72-byte pre-hash |
| User enumeration | Login and password-reset return identical responses and take similar time whether or not the account exists |

## Consequences

**Good.** No auth call on the request path — auth can be down and existing sessions
keep working. Blast radius of a compromised service is contained: it cannot forge
tokens. Key rotation needs no coordination.

**Costs.** Permission changes take up to 15 minutes to propagate (denylist covers the
urgent case). RSA verification is more CPU than HMAC — negligible next to a database
round trip, and it is cached per key. The auth service must guard its private key
absolutely; it is the one true secret on the platform.

**Rejected: storing the access token in `localStorage`.** Any XSS then yields a
long-lived credential. In memory it dies with the tab.

**Rejected: sessions in Redis checked per request.** It reintroduces a synchronous
dependency on every request across every service — the exact coupling this design
exists to avoid.
