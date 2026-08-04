"""Provider webhook endpoints.

Unauthenticated by design — the provider cannot hold one of our access tokens. The
signature *is* the authentication, which is why these handlers read the raw body
rather than a parsed model: re-serialising JSON changes bytes, and a signature is
over bytes.

That is also why there is no Pydantic request model here. FastAPI would parse and
re-encode the payload, and the signature check would then be verifying something the
provider never sent.

These routes always answer 2xx once the signature verifies, even when processing
failed. A 5xx makes the provider retry with backoff for days; the failure is recorded
on the ``webhook_events`` row and replayed deliberately instead.
"""

from __future__ import annotations

import orjson
from fastapi import APIRouter, Request, Response, status

from deps import DbSession, Orders, Payments, Webhooks
from knowledgeos_core import PaymentProvider, get_logger
from knowledgeos_core.deps import Ctx
from schemas import WebhookAck
from settings import settings

logger = get_logger(__name__)

router = APIRouter(prefix="/v1/webhooks", tags=["webhooks"])


async def _handle(
    request: Request,
    provider: PaymentProvider,
    session: DbSession,
    webhooks: Webhooks,
    orders: Orders,
    payments: Payments,
    ctx: Ctx,
    response: Response,
) -> WebhookAck:
    body = await request.body()
    if len(body) > settings.max_webhook_body_bytes:
        # A body this large is not a webhook. Refuse before parsing it.
        logger.warning("webhook.body_too_large", provider=provider.value, size=len(body))
        # Literal 413: Starlette has renamed this constant across versions.
        response.status_code = 413
        return WebhookAck(received=False)

    try:
        payload = orjson.loads(body) if body else {}
    except orjson.JSONDecodeError:
        logger.warning("webhook.malformed_json", provider=provider.value)
        response.status_code = status.HTTP_400_BAD_REQUEST
        return WebhookAck(received=False)
    if not isinstance(payload, dict):
        response.status_code = status.HTTP_400_BAD_REQUEST
        return WebhookAck(received=False)

    # Header names are matched case-insensitively downstream, so they are lowered
    # once here rather than at every lookup.
    headers = {key.lower(): value for key, value in request.headers.items()}

    outcome = await webhooks.ingest(
        session,
        provider=provider,
        body=body,
        headers=headers,
        payload=payload,
        publisher=ctx.publisher,
    )

    if not outcome.accepted:
        # 401, not 400: the request was well-formed but could not be authenticated.
        # Providers treat this as a configuration problem and surface it in their
        # dashboard, which is exactly where a missing webhook secret should show up.
        response.status_code = status.HTTP_401_UNAUTHORIZED
        return WebhookAck(received=False, event_id=outcome.event_id)

    # Events are published only after the webhook transaction has committed, so
    # nothing announces a payment that was rolled back.
    if outcome.settled_order_id is not None:
        order = await orders.get(session, outcome.settled_order_id)
        from services.payments import SettlementResult

        await payments.announce_paid(ctx.publisher, SettlementResult(order=order, newly_paid=True))

    return WebhookAck(received=True, event_id=outcome.event_id, duplicate=outcome.duplicate)


@router.post(
    "/razorpay",
    response_model=WebhookAck,
    summary="Razorpay webhook",
    description=(
        "Verified with `X-Razorpay-Signature`: HMAC-SHA256 of the raw body under the "
        "**webhook secret** (not the API secret — they are different values and "
        "using one for the other fails closed)."
    ),
)
async def razorpay_webhook(
    request: Request,
    response: Response,
    session: DbSession,
    webhooks: Webhooks,
    orders: Orders,
    payments: Payments,
    ctx: Ctx,
) -> WebhookAck:
    return await _handle(
        request, PaymentProvider.RAZORPAY, session, webhooks, orders, payments, ctx, response
    )


@router.post(
    "/stripe",
    response_model=WebhookAck,
    summary="Stripe webhook",
    description=(
        'Verified with `Stripe-Signature`. The signed message is `"<t>.<raw body>"`, '
        "so the timestamp is inside the signature — an old but validly-signed "
        "delivery is a replay and is rejected."
    ),
)
async def stripe_webhook(
    request: Request,
    response: Response,
    session: DbSession,
    webhooks: Webhooks,
    orders: Orders,
    payments: Payments,
    ctx: Ctx,
) -> WebhookAck:
    return await _handle(
        request, PaymentProvider.STRIPE, session, webhooks, orders, payments, ctx, response
    )
