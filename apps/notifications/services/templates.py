"""Template storage and rendering.

Rendering uses ``string.Template``-style ``{{name}}`` substitution and nothing else.
No Jinja, no expression evaluation, no attribute access. That is a deliberate
ceiling: templates are editable through the admin API, so a template language with
arbitrary evaluation would be remote code execution behind an admin token, and
server-side template injection is a well-trodden path from "content editor" to
"shell".

Missing variables **fail loudly**. A template that silently renders
``Hi {{first_name}},`` and mails it to a customer is worse than one that refuses to
render at all — the second is an alert, the first is an apology.
"""

from __future__ import annotations

import re
import uuid
from dataclasses import dataclass

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from knowledgeos_core import ConflictError, NotFoundError, NotificationChannel, get_logger
from models import Template
from schemas import TemplateCreate, TemplateUpdate
from settings import Settings

logger = get_logger(__name__)

#: ``{{ name }}`` with optional surrounding whitespace. Names are restricted to
#: identifiers, so a placeholder cannot smuggle in an expression.
PLACEHOLDER = re.compile(r"\{\{\s*([a-zA-Z_][a-zA-Z0-9_]*)\s*\}\}")


@dataclass(frozen=True, slots=True)
class RenderedMessage:
    subject: str | None
    body_text: str
    body_html: str | None
    missing: tuple[str, ...] = ()


def placeholders(text: str) -> set[str]:
    return set(PLACEHOLDER.findall(text or ""))


def render(text: str, variables: dict) -> tuple[str, set[str]]:
    """Substitute ``{{name}}``. Returns the result and any names that were missing.

    Values are stringified but never escaped here: escaping depends on the channel,
    and doing it in one place for both plain text and HTML would get one of them
    wrong. HTML escaping happens in :func:`render_message`.
    """
    missing: set[str] = set()

    def _replace(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in variables or variables[name] is None:
            missing.add(name)
            return match.group(0)
        return str(variables[name])

    return PLACEHOLDER.sub(_replace, text or ""), missing


def _escape_html(value: object) -> str:
    """Minimal HTML escaping for a substituted value.

    A book title containing ``<`` or an ampersand in a customer's name would
    otherwise break the markup — and a value that came from user input could inject
    a tag into a message the platform signed its name to.
    """
    text = str(value)
    return (
        text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;").replace('"', "&quot;")
    )


def render_message(template: Template, variables: dict) -> RenderedMessage:
    """Render every part of a template, escaping only the HTML body's values."""
    subject, missing = (None, set())
    if template.subject:
        subject, missing = render(template.subject, variables)

    body_text, text_missing = render(template.body_text, variables)
    missing |= text_missing

    body_html = None
    if template.body_html:
        escaped = {key: _escape_html(value) for key, value in variables.items()}
        body_html, html_missing = render(template.body_html, escaped)
        missing |= html_missing

    # Declared requirements are authoritative even when the body happens not to
    # reference them — a caller that forgets `order_number` should hear about it.
    for name in template.required_variables or []:
        if name not in variables or variables[name] is None:
            missing.add(str(name))

    return RenderedMessage(
        subject=subject,
        body_text=body_text,
        body_html=body_html,
        missing=tuple(sorted(missing)),
    )


class TemplateService:
    def __init__(self, settings: Settings) -> None:
        self._settings = settings

    async def find(
        self,
        session: AsyncSession,
        *,
        key: str,
        channel: NotificationChannel,
        locale: str = "en",
    ) -> Template | None:
        """Best match for (key, channel, locale), falling back to English.

        A missing translation should send the English message, not nothing. A
        customer would rather read a receipt in the wrong language than never
        receive one.
        """
        stmt = select(Template).where(
            Template.key == key,
            Template.channel == channel,
            Template.locale == locale,
            Template.is_active.is_(True),
        )
        found = (await session.execute(stmt)).scalars().one_or_none()
        if found is not None or locale == "en":
            return found
        return await self.find(session, key=key, channel=channel, locale="en")

    async def get(self, session: AsyncSession, template_id: uuid.UUID) -> Template:
        template = await session.get(Template, template_id)
        if template is None:
            raise NotFoundError("Template not found.", details={"template_id": str(template_id)})
        return template

    async def list(
        self, session: AsyncSession, *, key: str | None = None, active_only: bool = False
    ) -> list[Template]:
        conditions = []
        if key:
            conditions.append(Template.key == key)
        if active_only:
            conditions.append(Template.is_active.is_(True))
        stmt = select(Template).where(*conditions).order_by(Template.key, Template.channel)
        return list((await session.execute(stmt)).scalars().all())

    async def create(self, session: AsyncSession, payload: TemplateCreate) -> Template:
        existing = await self.find(
            session, key=payload.key, channel=payload.channel, locale=payload.locale
        )
        if existing is not None and existing.locale == payload.locale:
            raise ConflictError(
                "A template already exists for that key, channel and locale.",
                details={"key": payload.key, "channel": str(payload.channel)},
            )
        template = Template(
            key=payload.key,
            channel=payload.channel,
            locale=payload.locale,
            category=payload.category,
            subject=payload.subject,
            body_text=payload.body_text,
            body_html=payload.body_html,
            required_variables=list(payload.required_variables),
            is_active=payload.is_active,
            description=payload.description,
        )
        session.add(template)
        await session.commit()
        await session.refresh(template)
        logger.info("template.created", key=template.key, channel=str(template.channel))
        return template

    async def update(
        self, session: AsyncSession, template: Template, payload: TemplateUpdate
    ) -> Template:
        for field, value in payload.model_dump(exclude_unset=True).items():
            setattr(template, field, value)
        await session.commit()
        await session.refresh(template)
        return template

    async def delete(self, session: AsyncSession, template: Template) -> None:
        """Deactivates rather than deletes.

        Sent notifications reference the key, and a deleted template makes a past
        message unexplainable.
        """
        template.is_active = False
        await session.commit()
