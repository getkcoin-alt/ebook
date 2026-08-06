#!/usr/bin/env python3
"""Seed the notification templates the platform's own event routing asks for.

The notification service maps events to template *keys*; the templates themselves
are rows an operator creates. A fresh deployment therefore has a working pipeline
and nothing to send — every event is consumed, matched, and then abandoned with
"No active template for that key on any requested channel."

These are deliberately plain. Rendering is `{{name}}` substitution and nothing
else, and a missing variable aborts the send, so every template here references
only variables its event actually carries.
"""

from __future__ import annotations

import os
import sys

import httpx

GATEWAY = os.environ.get("KOS_GATEWAY", "https://gateway-production-c3e0.up.railway.app")

# (key, channel, category, subject, body_text, required_variables)
TEMPLATES = [
    (
        "account.welcome",
        "email",
        "account.welcome",
        "Welcome to KnowledgeOS",
        "Hi {{full_name}},\n\n"
        "Your KnowledgeOS account is ready — confirm your email address to finish "
        "setting it up:\n"
        "{{frontend_url}}/verify-email?token={{verification_token}}\n\n"
        "— The KnowledgeOS team",
        ["full_name", "verification_token"],
    ),
    (
        "account.welcome",
        "in_app",
        "account.welcome",
        "Welcome to KnowledgeOS",
        "Your account is ready. Start browsing the catalogue.",
        [],
    ),
    (
        "account.verified",
        "in_app",
        "account.security",
        "Email address confirmed",
        "Your email address has been confirmed.",
        [],
    ),
    (
        "account.password_reset",
        "email",
        "account.password_reset",
        "Reset your KnowledgeOS password",
        "Someone asked to reset the password for this account.\n\n"
        "Use this link within the next hour:\n"
        "{{frontend_url}}/reset-password?token={{reset_token}}\n\n"
        "If it wasn't you, ignore this message — your password has not changed.",
        ["reset_token"],
    ),
    (
        "order.receipt",
        "email",
        "order.receipt",
        "Your KnowledgeOS receipt",
        "Thanks for your order.\n\n"
        "Order {{order_number}}\nTotal: {{total_minor}} {{currency}} (minor units)\n\n"
        "Your books are in your library now and the tax invoice is available in "
        "your account.",
        ["order_number", "total_minor", "currency"],
    ),
    (
        "order.receipt",
        "in_app",
        "order.receipt",
        "Order confirmed",
        "Order {{order_number}} is paid. Your books are in your library.",
        ["order_number"],
    ),
    (
        "order.refund",
        "email",
        "order.refund",
        "Your KnowledgeOS refund",
        "A refund has been issued against order {{order_number}}.\n\n"
        "Refunded: {{amount_minor}} {{currency}} (minor units)\n\n"
        "It should reach your account within a few working days, depending on your "
        "bank.",
        ["order_number", "amount_minor", "currency"],
    ),
    (
        "order.refund",
        "in_app",
        "order.refund",
        "Refund issued",
        "A refund has been issued against order {{order_number}}.",
        ["order_number"],
    ),
    (
        "order.payment_failed",
        "email",
        "order.payment_failed",
        "Your payment did not go through",
        "The payment for order {{order_number}} did not complete, so the order is "
        "still waiting.\n\nNothing has been charged. You can try again from your "
        "orders page.",
        ["order_number"],
    ),
    (
        "order.payment_failed",
        "in_app",
        "order.payment_failed",
        "Payment did not complete",
        "Order {{order_number}} is still waiting for payment.",
        ["order_number"],
    ),
    (
        "subscription.activated",
        "email",
        "subscription.activated",
        "Your KnowledgeOS membership is active",
        "Your membership is active.\n\n"
        "Everything included in the plan is readable from your library straight "
        "away.",
        [],
    ),
    (
        "subscription.activated",
        "in_app",
        "subscription.activated",
        "Membership active",
        "Your membership is active.",
        [],
    ),
    (
        "subscription.cancelled",
        "email",
        "subscription.cancelled",
        "Your KnowledgeOS membership has been cancelled",
        "Your membership has been cancelled and will not renew.\n\n"
        "Anything you bought outright stays in your library.",
        [],
    ),
    (
        "subscription.cancelled",
        "in_app",
        "subscription.cancelled",
        "Membership cancelled",
        "Your membership will not renew.",
        [],
    ),
    (
        "book.published",
        "in_app",
        "book.published",
        "A new book is available",
        "{{title}} is now in the catalogue.",
        [],
    ),
    (
        "automation.failed",
        "in_app",
        "automation.failed",
        "An ingestion job failed",
        "An ingestion job failed. Check the automation dashboard for details.",
        [],
    ),
]


def main() -> int:
    with httpx.Client(base_url=GATEWAY, timeout=60.0) as c:
        r = c.post(
            "/v1/auth/login",
            json={
                "email": os.environ["KOS_ADMIN_EMAIL"],
                "password": os.environ["KOS_ADMIN_PASSWORD"],
            },
        )
        if r.status_code != 200:
            print("login failed:", r.status_code, r.text[:200])
            return 1
        h = {"Authorization": f"Bearer {r.json()['access_token']}"}

        created, existed, failed = 0, 0, []
        for key, channel, category, subject, body, variables in TEMPLATES:
            payload = {
                "key": key,
                "channel": channel,
                "category": category,
                "locale": "en",
                "subject": subject,
                "body_text": body,
                "required_variables": variables,
                "is_active": True,
                "description": f"Seeded default for {key} ({channel}).",
            }
            resp = c.post("/v1/admin/notifications/templates", headers=h, json=payload)
            if resp.status_code in (200, 201):
                created += 1
                print(f"  created  {key:<28} {channel}")
            elif resp.status_code == 409:
                # The row already exists — possibly deactivated. Update it in place
                # so seeding is idempotent and always leaves the current body active.
                tid = resp.json()["error"]["details"]["template_id"]
                patch = c.patch(
                    f"/v1/admin/notifications/templates/{tid}",
                    headers=h,
                    json={
                        "subject": subject,
                        "body_text": body,
                        "required_variables": variables,
                        "is_active": True,
                        "category": category,
                    },
                )
                if patch.status_code in (200, 201):
                    existed += 1
                    print(f"  updated  {key:<28} {channel}")
                else:
                    failed.append((key, channel, patch.status_code, patch.text[:180]))
                    print(f"  FAILED   {key:<28} {channel}  {patch.status_code} {patch.text[:160]}")
            else:
                failed.append((key, channel, resp.status_code, resp.text[:180]))
                print(f"  FAILED   {key:<28} {channel}  {resp.status_code} {resp.text[:160]}")

        print(f"\n{created} created, {existed} already present, {len(failed)} failed")

        listing = c.get("/v1/admin/notifications/templates", headers=h, params={"limit": 100})
        if listing.status_code == 200:
            body = listing.json()
            print(f"templates now in the database: {body.get('total', len(body.get('items', [])))}")
        return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
