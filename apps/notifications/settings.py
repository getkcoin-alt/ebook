"""Configuration for the notification service.

Two settings here have consequences beyond this service.

``suppression_enforced`` governs whether a hard bounce or a spam complaint
permanently blocks an address. It defaults on and should never be turned off: mail
sent to an address that already complained damages the sending domain's reputation
for *every* message the platform sends, including password resets.

``transactional_categories`` lists the messages a user cannot opt out of. Keep it
short and keep it honest — a receipt and a password reset belong there, a "new books
this week" digest does not.
"""

from __future__ import annotations

from pydantic import Field, computed_field

from knowledgeos_core import ServiceSettings
from knowledgeos_core.config import CsvList


class Settings(ServiceSettings):
    service_name: str = "notifications"
    database_schema: str = "notifications"
    port: int = 8006

    jwks_url: str | None = "http://localhost:8001/.well-known/jwks.json"

    # ---- sender identity -------------------------------------------------
    from_email: str = "noreply@knowledgeos.dev"
    from_name: str = "KnowledgeOS"
    #: Where a human reply actually goes. Sending from a no-reply address with no
    #: reply-to is a good way to lose a customer who hits reply.
    reply_to_email: str | None = None
    support_email: str = "support@knowledgeos.dev"

    # ---- email providers -------------------------------------------------
    #: "smtp" | "resend" | "console". `console` logs the message instead of sending
    #: it, which is what local development uses.
    email_provider: str = "console"

    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: str | None = None
    smtp_use_tls: bool = True
    smtp_timeout: float = 15.0

    resend_api_key: str | None = None
    #: Verifies inbound delivery webhooks. Without it they cannot be trusted, so
    #: they are refused rather than believed.
    resend_webhook_secret: str | None = None

    # ---- sms / whatsapp --------------------------------------------------
    twilio_account_sid: str | None = None
    twilio_auth_token: str | None = None
    twilio_from_number: str | None = None
    twilio_whatsapp_from: str | None = None

    # ---- push ------------------------------------------------------------
    fcm_server_key: str | None = None

    # ---- behaviour -------------------------------------------------------
    #: Categories a user may never opt out of. Legal and practical necessities.
    transactional_categories: CsvList = Field(
        default_factory=lambda: [
            "account.verify",
            "account.password_reset",
            "account.security",
            "order.receipt",
            "order.refund",
        ]
    )
    #: Attempts before a delivery is parked as permanently failed.
    max_delivery_attempts: int = 5
    #: Base seconds for exponential backoff between attempts.
    retry_base_seconds: int = 30
    provider_timeout: float = 20.0
    #: Cap on how many in-app rows one user accumulates. The oldest are pruned.
    in_app_retention: int = 500
    #: Days a delivery record is kept for support and deliverability debugging.
    delivery_retention_days: int = 90

    #: Off blocks every outbound send while still recording the intent — the switch
    #: to pull during an incident, or when restoring a database into staging and
    #: not wanting to re-mail every customer in it.
    sending_enabled: bool = True
    suppression_enforced: bool = True

    #: Signs unsubscribe links. A link carries a signed token, never a raw user id —
    #: otherwise anyone can unsubscribe anyone by editing a URL.
    unsubscribe_secret: str = "dev-unsubscribe-secret-change-me"  # noqa: S105 - a default, replaced in every real deployment
    unsubscribe_url: str = "http://localhost:3000/unsubscribe"

    # ---- events ----------------------------------------------------------
    events_enabled: bool = True
    event_consumer_group: str = "notifications"

    @computed_field  # type: ignore[prop-decorator]
    @property
    def email_enabled(self) -> bool:
        if self.email_provider == "console":
            return True
        if self.email_provider == "resend":
            return bool(self.resend_api_key)
        return bool(self.smtp_host)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def sms_enabled(self) -> bool:
        return bool(self.twilio_account_sid and self.twilio_auth_token and self.twilio_from_number)

    @computed_field  # type: ignore[prop-decorator]
    @property
    def whatsapp_enabled(self) -> bool:
        return bool(
            self.twilio_account_sid and self.twilio_auth_token and self.twilio_whatsapp_from
        )

    @computed_field  # type: ignore[prop-decorator]
    @property
    def push_enabled(self) -> bool:
        return bool(self.fcm_server_key)

    @property
    def enabled_channels(self) -> list[str]:
        channels = ["in_app"]  # always available; it is just a database row
        if self.email_enabled:
            channels.append("email")
        if self.sms_enabled:
            channels.append("sms")
        if self.whatsapp_enabled:
            channels.append("whatsapp")
        if self.push_enabled:
            channels.append("push")
        return channels


settings = Settings()
