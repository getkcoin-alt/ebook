"""Business logic for the notification service."""

from __future__ import annotations

from services.channels import ChannelRegistry, Message, SendResult
from services.dispatcher import (
    Dispatcher,
    DispatchResult,
    unsubscribe_token,
    verify_unsubscribe_token,
)
from services.events import NotificationEventHandler, register_consumers
from services.inbox import InboxService
from services.preferences import KNOWN_CATEGORIES, Decision, PreferenceService
from services.templates import (
    RenderedMessage,
    TemplateService,
    placeholders,
    render,
    render_message,
)

__all__ = [
    "KNOWN_CATEGORIES",
    "ChannelRegistry",
    "Decision",
    "DispatchResult",
    "Dispatcher",
    "InboxService",
    "Message",
    "NotificationEventHandler",
    "PreferenceService",
    "RenderedMessage",
    "SendResult",
    "TemplateService",
    "placeholders",
    "register_consumers",
    "render",
    "render_message",
    "unsubscribe_token",
    "verify_unsubscribe_token",
]
