"""Channel providers: email, SMS, WhatsApp, push.

Same shape as the payment gateways — one protocol, one registry, no branching on
provider name anywhere else in the service.

**Every provider distinguishes a retryable failure from a permanent one**, and that
distinction is the most important thing in this file. Retrying a hard bounce is how
a sending domain gets blocklisted: the receiving mail server has already said the
mailbox does not exist, and asking again several times an hour looks exactly like a
dictionary attack. `permanent=True` means stop, record a suppression, and never try
that address again.

The ``console`` provider is not a stub. It is what local development and CI use, and
it is the reason the whole send path — preferences, suppression, rendering, delivery
records, retries — can be exercised without a mail server.
"""

from __future__ import annotations

import base64
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Any, Protocol

import httpx

from knowledgeos_core import NotificationChannel, get_logger
from settings import Settings

logger = get_logger(__name__)


@dataclass(slots=True)
class SendResult:
    """What one provider attempt produced."""

    success: bool
    provider: str
    provider_message_id: str | None = None
    error: str | None = None
    #: True when retrying cannot possibly help — an invalid address, a rejected
    #: recipient, a malformed number. The caller suppresses rather than retries.
    permanent: bool = False
    raw: dict[str, Any] = field(default_factory=dict)


@dataclass(slots=True)
class Message:
    """A rendered message, ready for a channel."""

    destination: str
    subject: str | None
    body_text: str
    body_html: str | None = None
    #: Per-channel extras: push title/icon, WhatsApp template name, and so on.
    metadata: dict[str, Any] = field(default_factory=dict)


class ChannelProvider(Protocol):
    name: str
    channel: NotificationChannel

    @property
    def configured(self) -> bool: ...

    async def send(self, message: Message) -> SendResult: ...

    async def aclose(self) -> None: ...


# ---------------------------------------------------------------------------
# Email
# ---------------------------------------------------------------------------


class ConsoleEmailProvider:
    """Logs the message instead of sending it.

    The default, and what CI runs against. Everything upstream of the actual
    network call is exercised exactly as in production.
    """

    name = "console"
    channel = NotificationChannel.EMAIL

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return True

    async def send(self, message: Message) -> SendResult:
        logger.info(
            "email.console_send",
            to=message.destination,
            subject=message.subject,
            preview=message.body_text[:120],
        )
        # A deterministic id so a test can assert the delivery row recorded it.
        digest = base64.urlsafe_b64encode(
            f"{message.destination}:{message.subject}".encode()
        ).decode()[:24]
        return SendResult(success=True, provider=self.name, provider_message_id=f"console-{digest}")

    async def aclose(self) -> None:
        return None


class SMTPEmailProvider:
    """Plain SMTP over ``aiosmtplib``.

    Multipart alternative when an HTML body exists: text first, HTML second, which
    is the order the standard requires — a client picks the *last* part it can
    render, so reversing them shows raw HTML in a text-only reader.
    """

    name = "smtp"
    channel = NotificationChannel.EMAIL

    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    @property
    def configured(self) -> bool:
        return bool(self._settings.smtp_host)

    def _build(self, message: Message) -> EmailMessage:
        email = EmailMessage()
        email["From"] = f"{self._settings.from_name} <{self._settings.from_email}>"
        email["To"] = message.destination
        email["Subject"] = message.subject or ""
        if self._settings.reply_to_email:
            email["Reply-To"] = self._settings.reply_to_email
        if unsubscribe := message.metadata.get("unsubscribe_url"):
            # RFC 8058. Gmail and Yahoo require it on bulk mail, and a working
            # one-click unsubscribe is far better for deliverability than the
            # alternative the user reaches for otherwise: the spam button.
            email["List-Unsubscribe"] = f"<{unsubscribe}>"
            email["List-Unsubscribe-Post"] = "List-Unsubscribe=One-Click"

        email.set_content(message.body_text)
        if message.body_html:
            email.add_alternative(message.body_html, subtype="html")
        return email

    async def send(self, message: Message) -> SendResult:
        try:
            import aiosmtplib
        except ImportError:
            return SendResult(
                success=False,
                provider=self.name,
                error="aiosmtplib is not installed; set EMAIL_PROVIDER=console or install it.",
                permanent=True,
            )

        try:
            await aiosmtplib.send(
                self._build(message),
                hostname=self._settings.smtp_host,
                port=self._settings.smtp_port,
                username=self._settings.smtp_username,
                password=self._settings.smtp_password,
                start_tls=self._settings.smtp_use_tls,
                timeout=self._settings.smtp_timeout,
            )
        except Exception as exc:
            # A 5xx SMTP reply is permanent; anything else (connection refused, a
            # 4xx greylisting reply, a timeout) is worth retrying.
            code = getattr(exc, "code", None)
            permanent = isinstance(code, int) and 500 <= code < 600
            logger.warning(
                "email.smtp_failed", to=message.destination, error=str(exc), permanent=permanent
            )
            return SendResult(
                success=False, provider=self.name, error=str(exc)[:500], permanent=permanent
            )
        return SendResult(success=True, provider=self.name)

    async def aclose(self) -> None:
        return None


class ResendEmailProvider:
    """Resend's HTTP API."""

    name = "resend"
    channel = NotificationChannel.EMAIL
    API_BASE = "https://api.resend.com"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            base_url=self.API_BASE,
            timeout=httpx.Timeout(settings.provider_timeout, connect=5.0),
            headers={
                "Authorization": f"Bearer {settings.resend_api_key or ''}",
                "Content-Type": "application/json",
            },
        )

    @property
    def configured(self) -> bool:
        return bool(self._settings.resend_api_key)

    async def send(self, message: Message) -> SendResult:
        payload: dict[str, Any] = {
            "from": f"{self._settings.from_name} <{self._settings.from_email}>",
            "to": [message.destination],
            "subject": message.subject or "",
            "text": message.body_text,
        }
        if message.body_html:
            payload["html"] = message.body_html
        if self._settings.reply_to_email:
            payload["reply_to"] = self._settings.reply_to_email
        if unsubscribe := message.metadata.get("unsubscribe_url"):
            payload["headers"] = {
                "List-Unsubscribe": f"<{unsubscribe}>",
                "List-Unsubscribe-Post": "List-Unsubscribe=One-Click",
            }

        try:
            response = await self._client.post("/emails", json=payload)
        except httpx.HTTPError as exc:
            return SendResult(success=False, provider=self.name, error=str(exc)[:500])

        if response.is_success:
            data = response.json()
            return SendResult(
                success=True, provider=self.name, provider_message_id=str(data.get("id") or "")
            )

        # 4xx means the request itself is wrong — a malformed address, an
        # unverified sending domain. Retrying will produce the same 4xx forever.
        permanent = 400 <= response.status_code < 500 and response.status_code != 429
        return SendResult(
            success=False,
            provider=self.name,
            error=response.text[:500],
            permanent=permanent,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# SMS and WhatsApp
# ---------------------------------------------------------------------------


class TwilioProvider:
    """Twilio, for both SMS and WhatsApp — the API is the same with a prefix."""

    API_BASE = "https://api.twilio.com/2010-04-01"

    def __init__(
        self,
        settings: Settings,
        *,
        channel: NotificationChannel = NotificationChannel.SMS,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self.channel = channel
        self.name = "twilio"
        self._client = client or httpx.AsyncClient(
            base_url=self.API_BASE,
            timeout=httpx.Timeout(settings.provider_timeout, connect=5.0),
            auth=(settings.twilio_account_sid or "", settings.twilio_auth_token or ""),
        )

    @property
    def configured(self) -> bool:
        return (
            self._settings.whatsapp_enabled
            if self.channel is NotificationChannel.WHATSAPP
            else self._settings.sms_enabled
        )

    @property
    def _from(self) -> str:
        if self.channel is NotificationChannel.WHATSAPP:
            return f"whatsapp:{self._settings.twilio_whatsapp_from}"
        return self._settings.twilio_from_number or ""

    async def send(self, message: Message) -> SendResult:
        to = message.destination
        if self.channel is NotificationChannel.WHATSAPP and not to.startswith("whatsapp:"):
            to = f"whatsapp:{to}"

        try:
            response = await self._client.post(
                f"/Accounts/{self._settings.twilio_account_sid}/Messages.json",
                data={"From": self._from, "To": to, "Body": message.body_text[:1600]},
            )
        except httpx.HTTPError as exc:
            return SendResult(success=False, provider=self.name, error=str(exc)[:500])

        if response.is_success:
            data = response.json()
            return SendResult(
                success=True, provider=self.name, provider_message_id=str(data.get("sid") or "")
            )

        # Twilio 21211 is "invalid To number". Retrying a malformed number is
        # pointless and, for a number that belongs to someone else now, actively
        # rude.
        try:
            code = int(response.json().get("code", 0))
        except Exception:
            code = 0
        permanent = code in {21211, 21614, 21610} or (
            400 <= response.status_code < 500 and response.status_code != 429
        )
        return SendResult(
            success=False, provider=self.name, error=response.text[:500], permanent=permanent
        )

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# Push
# ---------------------------------------------------------------------------


class FCMProvider:
    """Firebase Cloud Messaging (legacy HTTP API)."""

    name = "fcm"
    channel = NotificationChannel.PUSH
    API_URL = "https://fcm.googleapis.com/fcm/send"

    def __init__(self, settings: Settings, client: httpx.AsyncClient | None = None) -> None:
        self._settings = settings
        self._client = client or httpx.AsyncClient(
            timeout=httpx.Timeout(settings.provider_timeout, connect=5.0),
            headers={
                "Authorization": f"key={settings.fcm_server_key or ''}",
                "Content-Type": "application/json",
            },
        )

    @property
    def configured(self) -> bool:
        return bool(self._settings.fcm_server_key)

    async def send(self, message: Message) -> SendResult:
        payload = {
            "to": message.destination,
            "notification": {
                "title": message.subject or self._settings.from_name,
                "body": message.body_text[:200],
                "icon": message.metadata.get("icon"),
                "click_action": message.metadata.get("action_url"),
            },
            "data": message.metadata.get("data") or {},
        }
        try:
            response = await self._client.post(self.API_URL, json=payload)
        except httpx.HTTPError as exc:
            return SendResult(success=False, provider=self.name, error=str(exc)[:500])

        if not response.is_success:
            return SendResult(
                success=False,
                provider=self.name,
                error=response.text[:500],
                permanent=400 <= response.status_code < 500 and response.status_code != 429,
            )

        data = response.json()
        # FCM answers 200 even when the token is dead. The real outcome is in the
        # body, and a NotRegistered token must be deactivated — continuing to push
        # to uninstalled apps is how a sender's FCM quota gets throttled.
        if int(data.get("failure", 0)) and data.get("results"):
            error = str(data["results"][0].get("error", "unknown"))
            return SendResult(
                success=False,
                provider=self.name,
                error=error,
                permanent=error in {"NotRegistered", "InvalidRegistration", "MismatchSenderId"},
                raw=data,
            )
        return SendResult(
            success=True,
            provider=self.name,
            provider_message_id=str(data.get("multicast_id") or ""),
            raw=data,
        )

    async def aclose(self) -> None:
        await self._client.aclose()


# ---------------------------------------------------------------------------
# In-app
# ---------------------------------------------------------------------------


class InAppProvider:
    """The in-app channel has no provider — the notification row *is* the delivery.

    It exists as a provider anyway so the dispatcher does not special-case it, and
    so a preference toggle for "in-app" behaves like every other channel.
    """

    name = "database"
    channel = NotificationChannel.IN_APP

    @property
    def configured(self) -> bool:
        return True

    async def send(self, message: Message) -> SendResult:
        return SendResult(success=True, provider=self.name)

    async def aclose(self) -> None:
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


class ChannelRegistry:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings
        self._providers: dict[NotificationChannel, ChannelProvider] = {
            NotificationChannel.IN_APP: InAppProvider(),
        }

        if settings.email_provider == "resend" and settings.resend_api_key:
            self._providers[NotificationChannel.EMAIL] = ResendEmailProvider(settings)
        elif settings.email_provider == "smtp" and settings.smtp_host:
            self._providers[NotificationChannel.EMAIL] = SMTPEmailProvider(settings)
        else:
            # Falls back to console rather than leaving email unavailable: a
            # half-configured deployment should still record what it would send.
            self._providers[NotificationChannel.EMAIL] = ConsoleEmailProvider(settings)

        if settings.sms_enabled:
            self._providers[NotificationChannel.SMS] = TwilioProvider(
                settings, channel=NotificationChannel.SMS
            )
        if settings.whatsapp_enabled:
            self._providers[NotificationChannel.WHATSAPP] = TwilioProvider(
                settings, channel=NotificationChannel.WHATSAPP
            )
        if settings.push_enabled:
            self._providers[NotificationChannel.PUSH] = FCMProvider(settings)

        logger.info(
            "notifications.channels_ready",
            channels=[channel.value for channel in self._providers],
            email_provider=self._providers[NotificationChannel.EMAIL].name,
        )

    def get(self, channel: NotificationChannel) -> ChannelProvider | None:
        return self._providers.get(channel)

    def __contains__(self, channel: object) -> bool:
        return channel in self._providers

    @property
    def available(self) -> list[NotificationChannel]:
        return list(self._providers)

    async def aclose(self) -> None:
        for provider in self._providers.values():
            await provider.aclose()
