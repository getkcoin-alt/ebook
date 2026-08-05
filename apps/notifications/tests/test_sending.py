"""Rendering, the send decision, delivery outcomes and retries.

The two properties that matter most: a half-rendered message never reaches a
customer, and an address that asked to be left alone never gets contacted again.
"""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import func, select

from knowledgeos_core import Event, EventType, NotificationChannel, ValidationError
from models import Delivery, Notification, Preference, Suppression
from schemas import DeliveryStatus, PreferenceUpdate, SendRequest, SuppressionReason
from services import render, render_message, unsubscribe_token, verify_unsubscribe_token
from tests.conftest import OTHER_ID, READER_ID, UNSUBSCRIBE_SECRET


async def _count(session, model) -> int:
    return int((await session.execute(select(func.count(model.id)))).scalar_one())


def _request(**overrides) -> SendRequest:
    payload = {
        "user_id": READER_ID,
        "template_key": "order.receipt",
        "variables": {"name": "Ada", "order_number": "KOS-1"},
        "channels": [NotificationChannel.IN_APP, NotificationChannel.EMAIL],
        "email": "ada@example.com",
    }
    payload.update(overrides)
    return SendRequest(**payload)


# ---------------------------------------------------------------------------
# Rendering
# ---------------------------------------------------------------------------


def test_substitution_replaces_named_placeholders():
    text, missing = render(
        "Hi {{name}}, order {{order_number}}.", {"name": "Ada", "order_number": "1"}
    )
    assert text == "Hi Ada, order 1."
    assert missing == set()


def test_whitespace_inside_a_placeholder_is_tolerated():
    text, _ = render("Hi {{  name  }}", {"name": "Ada"})
    assert text == "Hi Ada"


def test_a_missing_variable_is_reported_and_left_intact():
    """Left intact so the caller sees which placeholder failed, and reported so the
    dispatcher can refuse to send it."""
    text, missing = render("Hi {{name}}", {})
    assert missing == {"name"}
    assert "{{name}}" in text


def test_substituted_values_cannot_inject_a_placeholder():
    """A value containing `{{x}}` must not be re-substituted — otherwise a customer
    could put a placeholder in their display name and read another variable."""
    text, _ = render("Hi {{name}}", {"name": "{{secret}}", "secret": "leaked"})
    assert text == "Hi {{secret}}"


def test_html_values_are_escaped_but_text_values_are_not(template_factory):
    """Escaping in the text body would show a customer `&amp;` in their own name."""
    from models import Template

    template = Template(
        key="t",
        channel=NotificationChannel.EMAIL,
        locale="en",
        category="general",
        subject=None,
        body_text="Hi {{name}}",
        body_html="<p>Hi {{name}}</p>",
    )
    rendered = render_message(template, {"name": "Ada & <script>alert(1)</script>"})
    assert rendered.body_text == "Hi Ada & <script>alert(1)</script>"
    assert "&lt;script&gt;" in rendered.body_html
    assert "<script>" not in rendered.body_html


def test_declared_required_variables_are_enforced_even_when_unused():
    """A caller that forgets `order_number` should hear about it, whether or not
    the current copy happens to reference it."""
    from models import Template

    template = Template(
        key="t",
        channel=NotificationChannel.EMAIL,
        locale="en",
        category="general",
        subject=None,
        body_text="Static copy with no placeholders.",
        required_variables=["order_number"],
    )
    assert render_message(template, {}).missing == ("order_number",)


# ---------------------------------------------------------------------------
# The send path
# ---------------------------------------------------------------------------


async def test_a_send_writes_an_in_app_row_and_an_email_delivery(
    session, services, both_channel_templates, email_provider
):
    result = await services["dispatcher"].send(session, _request())

    assert result.notification is not None
    assert result.notification.title == "Your receipt for KOS-1"
    assert len(result.deliveries) == 1
    assert result.deliveries[0].status == DeliveryStatus.SENT
    assert email_provider.sent[0].destination == "ada@example.com"


async def test_a_missing_variable_aborts_the_send(session, services, both_channel_templates):
    """A customer receiving `Hi {{name}},` is an apology; a 422 is a bug report."""
    with pytest.raises(ValidationError) as exc:
        await services["dispatcher"].send(session, _request(variables={"name": "Ada"}))
    assert "order_number" in exc.value.details["missing"]

    assert await _count(session, Notification) == 0
    assert await _count(session, Delivery) == 0


async def test_a_missing_template_for_one_channel_does_not_block_the_other(
    session, services, template_factory, email_provider
):
    """A push message has no HTML body and a receipt has no push variant — a
    template existing for one channel and not another is normal."""
    await template_factory(channel=NotificationChannel.IN_APP)

    result = await services["dispatcher"].send(session, _request())
    assert result.notification is not None
    assert result.deliveries == []
    assert email_provider.sent == []


async def test_an_unknown_template_key_is_a_404(session, services):
    from knowledgeos_core import NotFoundError

    with pytest.raises(NotFoundError):
        await services["dispatcher"].send(session, _request(template_key="does.not.exist"))


async def test_every_message_carries_a_signed_unsubscribe_url(
    session, services, both_channel_templates, email_provider
):
    await services["dispatcher"].send(session, _request())
    url = email_provider.sent[0].metadata["unsubscribe_url"]
    token = url.split("token=")[1]
    assert verify_unsubscribe_token(UNSUBSCRIBE_SECRET, token) == READER_ID


async def test_account_links_can_be_built_without_hardcoding_the_domain(
    session, services, template_factory, email_provider
):
    """`frontend_url` is supplied to every template.

    The events that need a link carry only a bare token — `reset_token`,
    `verification_token` — and never a base URL. Without this variable every
    template has to hardcode the domain, and moving the frontend means editing
    every row rather than one setting.
    """
    await template_factory(
        key="account.password_reset",
        channel=NotificationChannel.EMAIL,
        category="account.password_reset",
        subject="Reset your password",
        body_text="Open {{frontend_url}}/reset-password?token={{reset_token}} to continue.",
        required_variables=["reset_token"],
    )
    await services["dispatcher"].send(
        session,
        _request(
            template_key="account.password_reset",
            variables={"reset_token": "tok-123"},
            channels=[NotificationChannel.EMAIL],
        ),
    )
    body = email_provider.sent[0].body_text
    assert "{{frontend_url}}" not in body
    assert "/reset-password?token=tok-123" in body


# ---------------------------------------------------------------------------
# Preferences and suppression
# ---------------------------------------------------------------------------


async def test_opting_out_skips_the_channel_and_records_why(
    session, services, template_factory, email_provider
):
    """A skipped delivery is recorded, not dropped — "why did they not get it?" has
    to be answerable from the database."""
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

    result = await services["dispatcher"].send(
        session, _request(channels=[NotificationChannel.EMAIL])
    )
    assert result.deliveries[0].status == DeliveryStatus.SKIPPED
    assert result.deliveries[0].error == "opted_out"
    assert email_provider.sent == []


async def test_a_transactional_message_ignores_an_opt_out(
    session, services, template_factory, email_provider
):
    """A receipt is not marketing."""
    await template_factory(channel=NotificationChannel.EMAIL, category="order.receipt")
    await services["preferences"].set(
        session,
        user_id=READER_ID,
        updates=[
            PreferenceUpdate(
                category="order.receipt", channel=NotificationChannel.EMAIL, enabled=False
            )
        ],
    )

    result = await services["dispatcher"].send(
        session, _request(channels=[NotificationChannel.EMAIL])
    )
    assert result.deliveries[0].status == DeliveryStatus.SENT
    assert len(email_provider.sent) == 1


async def test_disabling_a_transactional_category_is_not_even_stored(session, services):
    await services["preferences"].set(
        session,
        user_id=READER_ID,
        updates=[
            PreferenceUpdate(
                category="order.receipt", channel=NotificationChannel.EMAIL, enabled=False
            )
        ],
    )
    assert await _count(session, Preference) == 0


async def test_suppression_beats_even_a_transactional_message(
    session, services, template_factory, email_provider
):
    """The one rule that outranks everything. Mailing an address that issued a spam
    complaint costs the sending domain its reputation, and that takes password
    resets down with it."""
    await template_factory(channel=NotificationChannel.EMAIL, category="order.receipt")
    await services["preferences"].suppress(
        session,
        channel=NotificationChannel.EMAIL,
        destination="ada@example.com",
        reason=SuppressionReason.COMPLAINT,
    )

    result = await services["dispatcher"].send(
        session, _request(channels=[NotificationChannel.EMAIL])
    )
    assert result.deliveries[0].status == DeliveryStatus.SKIPPED
    assert "suppressed" in result.deliveries[0].error
    assert email_provider.sent == []


async def test_suppression_matching_is_case_insensitive(session, services):
    await services["preferences"].suppress(
        session,
        channel=NotificationChannel.EMAIL,
        destination="Ada@Example.COM",
        reason=SuppressionReason.HARD_BOUNCE,
    )
    found = await services["preferences"].suppression_for(
        session, channel=NotificationChannel.EMAIL, destination="ada@example.com"
    )
    assert found is not None


async def test_suppressing_twice_is_idempotent(session, services):
    for _ in range(2):
        await services["preferences"].suppress(
            session,
            channel=NotificationChannel.EMAIL,
            destination="ada@example.com",
            reason=SuppressionReason.HARD_BOUNCE,
        )
    assert await _count(session, Suppression) == 1


async def test_the_global_kill_switch_stops_everything(
    session, services, both_channel_templates, email_provider, settings, monkeypatch
):
    """What you pull when a production database has been restored into staging and
    must not re-mail every customer in it."""
    monkeypatch.setattr(settings, "sending_enabled", False)

    result = await services["dispatcher"].send(session, _request())
    assert result.notification is None
    assert email_provider.sent == []
    assert all(d.status == DeliveryStatus.SKIPPED for d in result.deliveries)


# ---------------------------------------------------------------------------
# Failures and retries
# ---------------------------------------------------------------------------


async def test_a_transient_failure_schedules_a_retry(
    session, services, both_channel_templates, email_provider
):
    email_provider.fail_mode = "transient"
    result = await services["dispatcher"].send(session, _request())

    delivery = result.deliveries[0]
    assert delivery.status == DeliveryStatus.FAILED
    assert delivery.attempts == 1
    assert delivery.next_attempt_at is not None


async def test_a_permanent_failure_suppresses_the_address(
    session, services, both_channel_templates, email_provider
):
    """Retrying a hard bounce is how a sending domain gets blocklisted: the
    receiving server already said the mailbox does not exist."""
    email_provider.fail_mode = "permanent"
    result = await services["dispatcher"].send(session, _request())

    assert result.deliveries[0].status == DeliveryStatus.BOUNCED
    assert result.deliveries[0].next_attempt_at is None

    suppression = await services["preferences"].suppression_for(
        session, channel=NotificationChannel.EMAIL, destination="ada@example.com"
    )
    assert suppression is not None
    assert suppression.reason == SuppressionReason.HARD_BOUNCE


async def test_backoff_grows_between_attempts(
    session, services, both_channel_templates, email_provider, settings
):
    """A provider having a bad minute should not receive the same volume of
    retries a second later."""
    email_provider.fail_mode = "transient"
    result = await services["dispatcher"].send(session, _request())
    delivery = result.deliveries[0]

    def _aware(value):
        # SQLite hands back naive datetimes where Postgres returns aware ones.
        return value if value.tzinfo else value.replace(tzinfo=UTC)

    first_gap = _aware(delivery.next_attempt_at) - datetime.now(UTC)
    delivery.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    await services["dispatcher"].retry_pending(session)
    await session.refresh(delivery)

    assert delivery.attempts == 2
    assert _aware(delivery.next_attempt_at) - datetime.now(UTC) > first_gap


async def test_retries_stop_at_the_attempt_ceiling(
    session, services, both_channel_templates, email_provider, settings, monkeypatch
):
    monkeypatch.setattr(settings, "max_delivery_attempts", 2)
    email_provider.fail_mode = "transient"

    result = await services["dispatcher"].send(session, _request())
    delivery = result.deliveries[0]
    delivery.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    await services["dispatcher"].retry_pending(session)
    await session.refresh(delivery)

    assert delivery.status == DeliveryStatus.BOUNCED
    assert delivery.next_attempt_at is None


async def test_a_retry_sends_the_same_message_not_a_truncated_one(
    session, services, template_factory, email_provider
):
    """Storing a preview and retrying from it would quietly mail customers half a
    message — a bug that only surfaces when a provider has a bad afternoon."""
    long_body = "Dear {{name}}, " + ("thank you for your order. " * 40) + "Order {{order_number}}."
    await template_factory(channel=NotificationChannel.EMAIL, body_text=long_body)

    email_provider.fail_mode = "transient"
    result = await services["dispatcher"].send(
        session, _request(channels=[NotificationChannel.EMAIL])
    )
    delivery = result.deliveries[0]
    delivery.next_attempt_at = datetime.now(UTC) - timedelta(seconds=1)
    await session.commit()

    email_provider.fail_mode = None
    await services["dispatcher"].retry_pending(session)

    assert len(email_provider.sent) == 2
    assert email_provider.sent[0].body_text == email_provider.sent[1].body_text
    assert len(email_provider.sent[1].body_text) > 500


async def test_a_retry_only_picks_up_deliveries_whose_backoff_elapsed(
    session, services, both_channel_templates, email_provider
):
    email_provider.fail_mode = "transient"
    await services["dispatcher"].send(session, _request())

    # next_attempt_at is in the future, so nothing is eligible yet.
    assert await services["dispatcher"].retry_pending(session) == 0


async def test_a_dead_push_token_is_deactivated(session, services, template_factory, push_provider):
    """Continuing to push to uninstalled apps gets a sender throttled by FCM."""
    from models import DeviceToken

    await template_factory(
        channel=NotificationChannel.PUSH, subject="Ping", body_text="Hello {{name}}"
    )
    session.add(DeviceToken(user_id=READER_ID, token="dead-token", platform="web"))
    await session.commit()

    push_provider.fail_mode = "permanent"
    await services["dispatcher"].send(
        session,
        _request(
            channels=[NotificationChannel.PUSH],
            variables={"name": "Ada", "order_number": "KOS-1"},
        ),
    )

    device = (
        (await session.execute(select(DeviceToken).where(DeviceToken.token == "dead-token")))
        .scalars()
        .one()
    )
    assert device.is_active is False


# ---------------------------------------------------------------------------
# Unsubscribe tokens
# ---------------------------------------------------------------------------


def test_an_unsubscribe_token_round_trips():
    token = unsubscribe_token(UNSUBSCRIBE_SECRET, READER_ID)
    assert verify_unsubscribe_token(UNSUBSCRIBE_SECRET, token) == READER_ID


def test_editing_the_id_in_a_token_invalidates_it():
    """Otherwise a URL containing an id lets anyone unsubscribe anyone."""
    token = unsubscribe_token(UNSUBSCRIBE_SECRET, READER_ID)
    _, _, signature = token.partition(".")
    forged = f"{OTHER_ID}.{signature}"
    assert verify_unsubscribe_token(UNSUBSCRIBE_SECRET, forged) is None


def test_a_token_signed_with_another_secret_is_rejected():
    token = unsubscribe_token("someone-elses-secret", READER_ID)
    assert verify_unsubscribe_token(UNSUBSCRIBE_SECRET, token) is None


def test_a_malformed_token_is_rejected_rather_than_raising():
    assert verify_unsubscribe_token(UNSUBSCRIBE_SECRET, "garbage") is None
    assert verify_unsubscribe_token(UNSUBSCRIBE_SECRET, "not-a-uuid.abc") is None


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------


async def test_an_order_paid_event_sends_a_receipt(
    session, services, settings, template_factory, email_provider
):
    from services import NotificationEventHandler

    await template_factory(key="order.receipt", channel=NotificationChannel.IN_APP)
    await template_factory(key="order.receipt", channel=NotificationChannel.EMAIL)

    handler = NotificationEventHandler(settings, services["dispatcher"])
    sent = await handler.handle(
        session,
        Event(
            type=EventType.ORDER_PAID,
            payload={
                "user_id": str(READER_ID),
                "order_number": "KOS-1",
                "name": "Ada",
                "email": "ada@example.com",
            },
        ),
    )
    assert sent is True
    assert len(email_provider.sent) == 1


async def test_a_redelivered_event_does_not_send_a_second_receipt(
    session, services, settings, template_factory, email_provider
):
    """Redis Streams deliver at least once. A duplicate `order.paid` must not
    charge the customer twice in their inbox."""
    from services import NotificationEventHandler

    await template_factory(key="order.receipt", channel=NotificationChannel.IN_APP)
    await template_factory(key="order.receipt", channel=NotificationChannel.EMAIL)
    handler = NotificationEventHandler(settings, services["dispatcher"])
    event = Event(
        type=EventType.ORDER_PAID,
        payload={
            "user_id": str(READER_ID),
            "order_number": "KOS-1",
            "name": "Ada",
            "email": "ada@example.com",
        },
    )

    assert await handler.handle(session, event) is True
    assert await handler.handle(session, event) is False
    assert len(email_provider.sent) == 1


async def test_an_event_without_a_user_id_is_ignored(session, services, settings):
    from services import NotificationEventHandler

    handler = NotificationEventHandler(settings, services["dispatcher"])
    assert (
        await handler.handle(
            session, Event(type=EventType.ORDER_PAID, payload={"order_number": "KOS-1"})
        )
        is False
    )


async def test_a_missing_template_does_not_dead_letter_the_event(session, services, settings):
    """A missing template must not block the consumer group behind it. The fix is
    to add the template and replay deliberately, not to retry five times."""
    from services import NotificationEventHandler

    handler = NotificationEventHandler(settings, services["dispatcher"])
    sent = await handler.handle(
        session,
        Event(
            type=EventType.ORDER_PAID,
            payload={"user_id": str(READER_ID), "email": "ada@example.com"},
        ),
    )
    assert sent is False


async def test_a_password_reset_goes_to_email_only(
    session, services, settings, template_factory, email_provider
):
    """Someone locked out of their account cannot read an in-app notification, and
    a reset link sitting in a session that may not be theirs is worse than useless."""
    from services import NotificationEventHandler

    await template_factory(
        key="account.password_reset",
        channel=NotificationChannel.EMAIL,
        category="account.password_reset",
        subject="Reset your password",
        body_text="Use {{reset_url}} to reset. {{name}} {{order_number}}",
    )
    handler = NotificationEventHandler(settings, services["dispatcher"])
    await handler.handle(
        session,
        Event(
            type=EventType.PASSWORD_RESET_REQUESTED,
            payload={
                "user_id": str(READER_ID),
                "email": "ada@example.com",
                "reset_url": "https://app.test/reset?t=x",
                "name": "Ada",
                "order_number": "-",
            },
        ),
    )
    assert len(email_provider.sent) == 1
    assert await _count(session, Notification) == 0


async def test_an_explicit_notification_request_carries_its_own_template(
    session, services, settings, template_factory
):
    from services import NotificationEventHandler

    await template_factory(key="custom.alert", channel=NotificationChannel.IN_APP)
    handler = NotificationEventHandler(settings, services["dispatcher"])
    sent = await handler.handle(
        session,
        Event(
            type=EventType.NOTIFICATION_REQUESTED,
            payload={
                "user_id": str(READER_ID),
                "template_key": "custom.alert",
                "channels": ["in_app"],
                "name": "Ada",
                "order_number": "KOS-9",
            },
        ),
    )
    assert sent is True
    assert await _count(session, Notification) == 1
