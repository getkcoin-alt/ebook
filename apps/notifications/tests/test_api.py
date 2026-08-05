"""HTTP surface: the bell, preferences, unsubscribe, templates and deliverability."""

from __future__ import annotations

from knowledgeos_core import NotificationChannel
from services import unsubscribe_token
from tests.conftest import OTHER_ID, READER_ID, UNSUBSCRIBE_SECRET


async def _seed_notification(session, user_id=READER_ID, **overrides):
    from models import Notification

    defaults = {
        "user_id": user_id,
        "template_key": "order.receipt",
        "category": "order.receipt",
        "title": "Your receipt",
        "body": "Thanks for your order.",
        "data": {},
    }
    defaults.update(overrides)
    notification = Notification(**defaults)
    session.add(notification)
    await session.commit()
    await session.refresh(notification)
    return notification


# ---------------------------------------------------------------------------
# Inbox
# ---------------------------------------------------------------------------


async def test_the_inbox_requires_authentication(client):
    assert (await client.get("/v1/notifications")).status_code == 401
    assert (await client.get("/v1/notifications/unread-count")).status_code == 401


async def test_you_only_see_your_own_notifications(client, session, as_user):
    await _seed_notification(session, user_id=OTHER_ID)
    as_user(READER_ID)
    assert (await client.get("/v1/notifications")).json()["items"] == []


async def test_the_unread_count_is_its_own_endpoint(client, session, as_user):
    await _seed_notification(session)
    await _seed_notification(session)
    as_user(READER_ID)

    assert (await client.get("/v1/notifications/unread-count")).json()["unread"] == 2


async def test_marking_all_read_clears_the_badge(client, session, as_user):
    await _seed_notification(session)
    await _seed_notification(session)
    as_user(READER_ID)

    response = await client.post("/v1/notifications/read", json={})
    assert response.status_code == 200
    assert (await client.get("/v1/notifications/unread-count")).json()["unread"] == 0


async def test_marking_one_read_leaves_the_rest(client, session, as_user):
    first = await _seed_notification(session)
    await _seed_notification(session)
    as_user(READER_ID)

    await client.post("/v1/notifications/read", json={"notification_ids": [str(first.id)]})
    assert (await client.get("/v1/notifications/unread-count")).json()["unread"] == 1


async def test_you_cannot_mark_someone_elses_notification_read(client, session, as_user):
    theirs = await _seed_notification(session, user_id=OTHER_ID)
    as_user(READER_ID)

    await client.post("/v1/notifications/read", json={"notification_ids": [str(theirs.id)]})
    await session.refresh(theirs)
    assert theirs.read_at is None


async def test_archiving_also_marks_it_read(client, session, as_user):
    """Archiving is an acknowledgement; leaving the badge lit for something the
    user explicitly dismissed is just wrong."""
    notification = await _seed_notification(session)
    as_user(READER_ID)

    response = await client.post(f"/v1/notifications/{notification.id}/archive")
    assert response.status_code == 200
    assert response.json()["read_at"] is not None
    assert (await client.get("/v1/notifications")).json()["items"] == []


async def test_archiving_someone_elses_notification_is_a_404(client, session, as_user):
    theirs = await _seed_notification(session, user_id=OTHER_ID)
    as_user(READER_ID)
    assert (await client.post(f"/v1/notifications/{theirs.id}/archive")).status_code == 404


async def test_the_inbox_paginates(client, session, as_user):
    for _ in range(5):
        await _seed_notification(session)
    as_user(READER_ID)

    first = (await client.get("/v1/notifications?limit=2")).json()
    assert len(first["items"]) == 2
    assert first["has_more"] is True

    second = (await client.get(f"/v1/notifications?limit=2&cursor={first['next_cursor']}")).json()
    assert {n["id"] for n in first["items"]}.isdisjoint({n["id"] for n in second["items"]})


async def test_unread_only_filters_the_list(client, session, as_user):
    from datetime import UTC, datetime

    await _seed_notification(session, read_at=datetime.now(UTC))
    await _seed_notification(session)
    as_user(READER_ID)

    assert len((await client.get("/v1/notifications?unread_only=true")).json()["items"]) == 1


# ---------------------------------------------------------------------------
# Preferences
# ---------------------------------------------------------------------------


async def test_preferences_default_to_on_without_a_stored_row(client, as_user):
    """There is no row per user per category at signup: writing millions of rows
    that all say "yes" makes the table useless."""
    as_user(READER_ID)
    body = (await client.get("/v1/notifications/preferences")).json()
    assert body["preferences"]
    assert all(item["enabled"] for item in body["preferences"])


async def test_transactional_categories_come_back_locked(client, as_user):
    as_user(READER_ID)
    body = (await client.get("/v1/notifications/preferences")).json()
    receipt = next(
        item
        for item in body["preferences"]
        if item["category"] == "order.receipt" and item["channel"] == "email"
    )
    assert receipt["locked"] is True

    digest = next(
        item
        for item in body["preferences"]
        if item["category"] == "marketing.digest" and item["channel"] == "email"
    )
    assert digest["locked"] is False


async def test_a_preference_can_be_turned_off_and_read_back(client, as_user):
    as_user(READER_ID)
    response = await client.put(
        "/v1/notifications/preferences",
        json={
            "preferences": [{"category": "marketing.digest", "channel": "email", "enabled": False}]
        },
    )
    assert response.status_code == 200

    body = (await client.get("/v1/notifications/preferences")).json()
    digest = next(
        item
        for item in body["preferences"]
        if item["category"] == "marketing.digest" and item["channel"] == "email"
    )
    assert digest["enabled"] is False


# ---------------------------------------------------------------------------
# Unsubscribe
# ---------------------------------------------------------------------------


async def test_unsubscribe_works_without_a_token_of_your_own(client):
    """Reached from an email footer by someone who may not be signed in."""
    token = unsubscribe_token(UNSUBSCRIBE_SECRET, READER_ID)
    response = await client.post("/v1/notifications/unsubscribe", json={"token": token})
    assert response.status_code == 200
    assert "unsubscribed" in response.json()["message"].lower()


async def test_a_forged_unsubscribe_token_is_refused(client):
    """Otherwise a URL containing an id lets anyone unsubscribe anyone."""
    token = unsubscribe_token(UNSUBSCRIBE_SECRET, READER_ID)
    _, _, signature = token.partition(".")
    response = await client.post(
        "/v1/notifications/unsubscribe", json={"token": f"{OTHER_ID}.{signature}"}
    )
    assert response.status_code == 400
    assert response.json()["error"]["code"] == "invalid_unsubscribe_token"


async def test_unsubscribing_from_a_transactional_category_says_so_plainly(client):
    """A silent success would leave the user expecting the mail to stop."""
    token = unsubscribe_token(UNSUBSCRIBE_SECRET, READER_ID)
    response = await client.post(
        "/v1/notifications/unsubscribe", json={"token": token, "category": "order.receipt"}
    )
    assert response.status_code == 200
    assert response.json()["success"] is False
    assert "cannot be turned off" in response.json()["message"]


async def test_a_blanket_unsubscribe_leaves_transactional_messages_on(client, session, as_user):
    token = unsubscribe_token(UNSUBSCRIBE_SECRET, READER_ID)
    await client.post("/v1/notifications/unsubscribe", json={"token": token})

    as_user(READER_ID)
    body = (await client.get("/v1/notifications/preferences")).json()
    by_key = {(item["category"], item["channel"]): item for item in body["preferences"]}
    assert by_key[("marketing.digest", "email")]["enabled"] is False
    assert by_key[("order.receipt", "email")]["enabled"] is True


# ---------------------------------------------------------------------------
# Devices
# ---------------------------------------------------------------------------


async def test_registering_a_push_token_is_idempotent(client, as_user):
    as_user(READER_ID)
    first = await client.post(
        "/v1/notifications/devices", json={"token": "abc12345", "platform": "web"}
    )
    second = await client.post(
        "/v1/notifications/devices", json={"token": "abc12345", "platform": "web"}
    )
    assert first.status_code == 201
    assert first.json()["id"] == second.json()["id"]


async def test_re_registering_a_token_reassigns_it(client, session, as_user):
    """A shared device where one user signs out and another signs in must not keep
    pushing to the first."""
    from sqlalchemy import select

    from models import DeviceToken

    as_user(READER_ID)
    await client.post("/v1/notifications/devices", json={"token": "shared-token"})

    as_user(OTHER_ID)
    await client.post("/v1/notifications/devices", json={"token": "shared-token"})

    device = (
        (await session.execute(select(DeviceToken).where(DeviceToken.token == "shared-token")))
        .scalars()
        .one()
    )
    assert device.user_id == OTHER_ID


async def test_channels_endpoint_reports_what_is_configured(client):
    body = (await client.get("/v1/notifications/channels")).json()
    assert "in_app" in body["channels"]
    assert body["sending_enabled"] is True


# ---------------------------------------------------------------------------
# Admin
# ---------------------------------------------------------------------------


async def test_admin_routes_reject_an_ordinary_user(client, as_user):
    as_user()
    assert (await client.get("/v1/admin/notifications/templates")).status_code == 403
    assert (await client.get("/v1/admin/notifications/stats")).status_code == 403


async def test_an_admin_can_create_and_preview_a_template(client, as_admin):
    as_admin()
    created = await client.post(
        "/v1/admin/notifications/templates",
        json={
            "key": "welcome.mail",
            "channel": "email",
            "category": "account.welcome",
            "subject": "Welcome {{name}}",
            "body_text": "Hello {{name}}, glad you are here.",
        },
    )
    assert created.status_code == 201

    preview = await client.post(
        f"/v1/admin/notifications/templates/{created.json()['id']}/preview",
        json={"variables": {"name": "Ada"}},
    )
    assert preview.json()["subject"] == "Welcome Ada"
    assert preview.json()["missing_variables"] == []


async def test_a_preview_names_the_variables_it_is_missing(client, as_admin):
    """What every copy change should go through before it reaches a customer."""
    as_admin()
    created = await client.post(
        "/v1/admin/notifications/templates",
        json={
            "key": "welcome.mail",
            "channel": "email",
            "subject": "Welcome {{name}}",
            "body_text": "Order {{order_number}} for {{name}}.",
        },
    )
    preview = await client.post(
        f"/v1/admin/notifications/templates/{created.json()['id']}/preview",
        json={"variables": {}},
    )
    assert set(preview.json()["missing_variables"]) == {"name", "order_number"}


async def test_a_duplicate_template_is_a_409(client, as_admin):
    as_admin()
    body = {"key": "dupe.mail", "channel": "email", "body_text": "hi"}
    await client.post("/v1/admin/notifications/templates", json=body)
    assert (await client.post("/v1/admin/notifications/templates", json=body)).status_code == 409


async def test_recreating_a_deactivated_template_is_a_409_not_a_500(client, as_admin):
    """Deactivate, then create a replacement — the obvious way to revise a template.

    The duplicate guard used the same lookup the dispatcher uses, which filters on
    `is_active`; the unique constraint does not. So the guard passed, the insert hit
    the constraint, and an operator saw an opaque 500. The answer they need is that
    the row is still there and can be reactivated.
    """
    as_admin()
    body = {"key": "revised.mail", "channel": "email", "body_text": "first"}
    created = await client.post("/v1/admin/notifications/templates", json=body)
    assert created.status_code == 201
    template_id = created.json()["id"]

    assert (
        await client.delete(f"/v1/admin/notifications/templates/{template_id}")
    ).status_code in (200, 204)

    again = await client.post(
        "/v1/admin/notifications/templates",
        json={**body, "body_text": "second"},
    )
    assert again.status_code == 409, again.text
    details = again.json()["error"]["details"]
    assert details["is_active"] is False
    assert details["template_id"] == template_id
    assert "reactivate" in again.json()["error"]["message"].lower()


async def test_a_template_key_with_illegal_characters_is_a_422(client, as_admin):
    as_admin()
    response = await client.post(
        "/v1/admin/notifications/templates",
        json={"key": "bad key!", "channel": "email", "body_text": "hi"},
    )
    assert response.status_code == 422


async def test_deleting_a_template_deactivates_it(client, as_admin):
    """Sent notifications reference the key; a deleted template makes a past
    message unexplainable."""
    as_admin()
    created = await client.post(
        "/v1/admin/notifications/templates",
        json={"key": "temp.mail", "channel": "email", "body_text": "hi"},
    )
    template_id = created.json()["id"]
    assert (
        await client.delete(f"/v1/admin/notifications/templates/{template_id}")
    ).status_code == 200

    listed = (await client.get("/v1/admin/notifications/templates")).json()
    assert listed["items"][0]["is_active"] is False


async def test_an_admin_can_block_and_unblock_an_address(client, as_admin):
    as_admin()
    created = await client.post(
        "/v1/admin/notifications/suppressions",
        json={"destination": "bounced@example.com", "reason": "hard_bounce"},
    )
    assert created.status_code == 201

    listed = (await client.get("/v1/admin/notifications/suppressions")).json()
    assert listed["total"] == 1

    lifted = await client.delete(
        "/v1/admin/notifications/suppressions?destination=bounced@example.com"
    )
    assert lifted.json()["success"] is True


async def test_delivery_stats_exclude_skipped_from_the_bounce_rate(client, session, as_admin):
    """Including them would understate the bounce rate exactly when a suppression
    list is growing."""
    from models import Delivery
    from schemas import DeliveryStatus

    for status in (DeliveryStatus.SENT, DeliveryStatus.BOUNCED, DeliveryStatus.SKIPPED):
        session.add(
            Delivery(
                user_id=READER_ID,
                channel=NotificationChannel.EMAIL,
                destination="a@example.com",
                status=status,
                body_text="body",
            )
        )
    await session.commit()

    as_admin()
    stats = (await client.get("/v1/admin/notifications/stats")).json()
    assert stats["total"] == 3
    assert stats["skipped"] == 1
    # 1 bounced out of 2 attempted, not out of 3 total.
    assert stats["bounce_rate"] == 0.5


# ---------------------------------------------------------------------------
# Internal
# ---------------------------------------------------------------------------


async def test_internal_send_requires_a_signature(client):
    response = await client.post(
        "/internal/send", json={"user_id": str(READER_ID), "template_key": "x"}
    )
    assert response.status_code == 401


async def test_a_service_can_ask_for_a_message_by_template_key(
    client, as_internal, template_factory, email_provider
):
    await template_factory(channel=NotificationChannel.IN_APP)
    await template_factory(channel=NotificationChannel.EMAIL)
    as_internal("payment")

    response = await client.post(
        "/internal/send",
        json={
            "user_id": str(READER_ID),
            "template_key": "order.receipt",
            "variables": {"name": "Ada", "order_number": "KOS-1"},
            "email": "ada@example.com",
        },
    )
    assert response.status_code == 200
    body = response.json()
    assert body["notification_id"] is not None
    assert body["deliveries"][0]["status"] == "sent"


async def test_the_send_response_reports_skipped_channels_and_why(
    client, as_internal, session, template_factory, services
):
    """A caller that only sees successes cannot tell "delivered" from "silently
    dropped because the user opted out"."""
    from schemas import PreferenceUpdate

    await template_factory(channel=NotificationChannel.EMAIL, category="marketing.digest")
    await services["preferences"].set(
        session,
        user_id=READER_ID,
        updates=[
            PreferenceUpdate(
                category="marketing.digest", channel=NotificationChannel.EMAIL, enabled=False
            )
        ],
    )
    as_internal("admin")

    response = await client.post(
        "/internal/send",
        json={
            "user_id": str(READER_ID),
            "template_key": "order.receipt",
            "variables": {"name": "Ada", "order_number": "KOS-1"},
            "channels": ["email"],
            "email": "ada@example.com",
        },
    )
    delivery = response.json()["deliveries"][0]
    assert delivery["status"] == "skipped"
    assert delivery["error"] == "opted_out"


async def test_an_unknown_template_over_internal_is_a_404(client, as_internal):
    """Loud, because a silent success means a receipt nobody notices is missing."""
    as_internal("payment")
    response = await client.post(
        "/internal/send", json={"user_id": str(READER_ID), "template_key": "nope"}
    )
    assert response.status_code == 404


async def test_the_worker_can_drive_retries(client, as_internal):
    as_internal("worker")
    response = await client.post("/internal/maintenance/retry")
    assert response.status_code == 200
    assert "Retried" in response.json()["message"]


async def test_health_and_metrics_are_public(client):
    assert (await client.get("/health")).status_code == 200
    assert (await client.get("/metrics")).status_code == 200
