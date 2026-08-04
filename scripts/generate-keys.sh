#!/usr/bin/env bash
# Generate the RSA keypair the auth service signs access tokens with.
#
#   ./scripts/generate-keys.sh            # print PEMs plus paste-ready one-liners
#   ./scripts/generate-keys.sh --env      # print only the two env-var lines
#
# The private key is the single most sensitive secret on the platform: whoever
# holds it can mint an admin token for any user. Set it ONLY on the auth service.

set -euo pipefail

BITS="${KEY_BITS:-2048}"
MODE="${1:-}"

command -v openssl >/dev/null 2>&1 || { echo "openssl is required." >&2; exit 1; }

tmp="$(mktemp -d)"
trap 'rm -rf "$tmp"' EXIT
chmod 700 "$tmp"

openssl genpkey -algorithm RSA -pkeyopt "rsa_keygen_bits:${BITS}" -out "$tmp/private.pem" 2>/dev/null
openssl rsa -in "$tmp/private.pem" -pubout -out "$tmp/public.pem" 2>/dev/null

# Railway (and most secret stores) cannot hold a literal newline in a variable,
# so also emit an escaped single-line form. settings.py un-escapes it on read.
escape() { awk '{printf "%s\\n", $0}' "$1"; }

if [ "$MODE" = "--env" ]; then
  printf 'JWT_PRIVATE_KEY="%s"\n' "$(escape "$tmp/private.pem")"
  printf 'JWT_PUBLIC_KEY="%s"\n' "$(escape "$tmp/public.pem")"
  exit 0
fi

cat <<BANNER
=============================================================================
 KnowledgeOS signing keypair (RSA-${BITS})
=============================================================================

  * Set JWT_PRIVATE_KEY on the AUTH SERVICE ONLY.
  * Every other service verifies via /.well-known/jwks.json — it needs neither key.
  * Never commit these. Never log them. Never paste them into a chat or an issue.

--- PRIVATE KEY -------------------------------------------------------------
BANNER
cat "$tmp/private.pem"
echo "--- PUBLIC KEY --------------------------------------------------------------"
cat "$tmp/public.pem"

cat <<'BANNER'

--- Paste-ready environment variables ---------------------------------------
(newlines escaped as \n, which is what the Railway dashboard accepts)

BANNER
printf 'JWT_PRIVATE_KEY="%s"\n\n' "$(escape "$tmp/private.pem")"
printf 'JWT_PUBLIC_KEY="%s"\n\n' "$(escape "$tmp/public.pem")"

cat <<'BANNER'
--- Rotating ----------------------------------------------------------------
1. Generate a new pair with this script.
2. Move the CURRENT public key to JWT_PREVIOUS_PUBLIC_KEY.
3. Set the new pair as JWT_PRIVATE_KEY / JWT_PUBLIC_KEY and deploy.
   JWKS publishes both, so tokens signed before the swap keep verifying.
4. After one access-token lifetime (default 15 min), clear
   JWT_PREVIOUS_PUBLIC_KEY.

No coordinated deploy is needed: verifiers pick the key by the token's `kid`.
BANNER
