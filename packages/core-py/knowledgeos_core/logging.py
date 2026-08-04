"""Structured logging.

One configuration for the whole platform: JSON in deployed environments, coloured
key-value output locally. Every log line automatically carries the request id, the
authenticated user id and the service name via contextvars, so a single grep across
Railway logs reconstructs a full request path.
"""

from __future__ import annotations

import logging
import sys
from collections.abc import MutableMapping
from contextvars import ContextVar
from typing import Any

import orjson
import structlog

#: Correlation id propagated across services through the ``X-Request-ID`` header.
request_id_ctx: ContextVar[str | None] = ContextVar("request_id", default=None)
user_id_ctx: ContextVar[str | None] = ContextVar("user_id", default=None)
trace_id_ctx: ContextVar[str | None] = ContextVar("trace_id", default=None)

#: Keys whose values are replaced with ``***`` before a log line is emitted.
REDACTED_KEYS = frozenset(
    {
        "password",
        "new_password",
        "current_password",
        "token",
        "access_token",
        "refresh_token",
        "id_token",
        "authorization",
        "api_key",
        "secret",
        "client_secret",
        "private_key",
        "card",
        "card_number",
        "cvv",
        "otp",
        "totp_secret",
        "signature",
        "cookie",
        "set-cookie",
    }
)


def _add_context(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Attach ambient correlation identifiers to every event."""
    if (rid := request_id_ctx.get()) is not None:
        event_dict.setdefault("request_id", rid)
    if (uid := user_id_ctx.get()) is not None:
        event_dict.setdefault("user_id", uid)
    if (tid := trace_id_ctx.get()) is not None:
        event_dict.setdefault("trace_id", tid)
    return event_dict


def _redact(
    _logger: Any, _method: str, event_dict: MutableMapping[str, Any]
) -> MutableMapping[str, Any]:
    """Best-effort scrub of credential-shaped values.

    This is a safety net, not a licence to log secrets: the rule remains "never pass
    a secret to a logger". It only walks the top level plus one nested dict, which
    keeps the hot path cheap.
    """
    for key in list(event_dict.keys()):
        lowered = key.lower()
        if lowered in REDACTED_KEYS:
            event_dict[key] = "***"
        elif isinstance(event_dict[key], dict):
            nested = event_dict[key]
            for nested_key in list(nested.keys()):
                if nested_key.lower() in REDACTED_KEYS:
                    nested[nested_key] = "***"
    return event_dict


def _orjson_dumps(obj: Any, default: Any = None, **_: Any) -> str:
    return orjson.dumps(obj, default=default).decode()


def configure_logging(
    *,
    service_name: str,
    level: str = "INFO",
    fmt: str = "json",
    version: str = "0.1.0",
    environment: str = "local",
) -> None:
    """Install the platform logging configuration. Safe to call more than once."""
    renderer: Any
    if fmt == "json":
        renderer = structlog.processors.JSONRenderer(serializer=_orjson_dumps)
    else:
        renderer = structlog.dev.ConsoleRenderer(colors=sys.stderr.isatty())

    shared_processors: list[Any] = [
        structlog.contextvars.merge_contextvars,
        structlog.stdlib.add_log_level,
        structlog.stdlib.add_logger_name,
        structlog.processors.TimeStamper(fmt="iso", utc=True),
        structlog.processors.StackInfoRenderer(),
        structlog.processors.UnicodeDecoder(),
        _add_context,
        _redact,
    ]

    # Both structlog calls and stdlib calls must end up rendered exactly once, by the
    # same renderer. structlog therefore stops at `wrap_for_formatter`, which hands
    # the event dict to stdlib instead of rendering it; ProcessorFormatter on the
    # handler does the single final render. Rendering in both places is what produces
    # JSON-inside-JSON log lines.
    structlog.configure(
        processors=[*shared_processors, structlog.stdlib.ProcessorFormatter.wrap_for_formatter],
        wrapper_class=structlog.make_filtering_bound_logger(
            logging.getLevelNamesMapping()[level.upper()]
        ),
        logger_factory=structlog.stdlib.LoggerFactory(),
        cache_logger_on_first_use=True,
    )

    # Route stdlib logging (uvicorn, sqlalchemy, celery, boto) through the same
    # pipeline so the deployed log stream is uniformly parseable.
    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(
        structlog.stdlib.ProcessorFormatter(
            # foreign_pre_chain runs only for records that did NOT come from
            # structlog, giving third-party logs the same context and redaction.
            foreign_pre_chain=shared_processors,
            processors=[
                structlog.stdlib.ProcessorFormatter.remove_processors_meta,
                structlog.processors.format_exc_info,
                renderer,
            ],
        )
    )
    root = logging.getLogger()
    root.handlers = [handler]
    root.setLevel(level.upper())

    for noisy, noisy_level in (
        ("uvicorn.access", logging.WARNING),  # our middleware logs access lines
        ("uvicorn.error", logging.INFO),
        ("botocore", logging.WARNING),
        ("boto3", logging.WARNING),
        ("urllib3", logging.WARNING),
        ("httpx", logging.WARNING),
        ("httpcore", logging.WARNING),
        ("asyncio", logging.WARNING),
        ("multipart", logging.WARNING),
    ):
        logging.getLogger(noisy).setLevel(noisy_level)

    structlog.contextvars.bind_contextvars(service=service_name, version=version, env=environment)


def get_logger(name: str | None = None) -> structlog.stdlib.BoundLogger:
    """Return a bound structlog logger. Use module ``__name__`` as the name."""
    return structlog.get_logger(name)  # type: ignore[no-any-return]
