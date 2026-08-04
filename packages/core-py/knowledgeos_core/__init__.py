"""KnowledgeOS shared runtime.

Everything a KnowledgeOS Python service needs in order to look, log, fail and shut
down like every other service on the platform.

Typical service entrypoint::

    from knowledgeos_core import Components, ServiceSettings, create_app

    class Settings(ServiceSettings):
        service_name: str = "books"
        database_schema: str = "books"

    settings = Settings()
    app = create_app(
        settings=settings,
        components=Components(database=True, redis=True, auth=True, events=True),
        routers=[books_router],
    )
"""

from __future__ import annotations

from .app import AppContext, Components, create_app, run
from .config import ServiceSettings
from .db import Base, Database, SoftDeleteMixin, TimestampMixin, UUIDPrimaryKeyMixin
from .errors import (
    AppError,
    BadRequestError,
    ConflictError,
    ForbiddenError,
    NotFoundError,
    PaymentRequiredError,
    RateLimitedError,
    ServiceUnavailableError,
    UnauthorizedError,
    UnsupportedMediaTypeError,
    UpstreamError,
    ValidationError,
)
from .events import Event, EventConsumer, EventPublisher, EventType
from .logging import configure_logging, get_logger
from .pagination import (
    CursorPage,
    Page,
    PageParams,
    apply_search,
    apply_sorting,
    decode_cursor,
    encode_cursor,
    page_params,
    paginate,
)
from .schemas import (
    ROLE_PERMISSIONS,
    BaseSchema,
    BookFormat,
    BookStatus,
    Currency,
    ErrorResponse,
    JobStatus,
    MessageResponse,
    NotificationChannel,
    OrderStatus,
    PaymentProvider,
    Permission,
    UserRole,
)
from .security import Principal, generate_token, hash_password, hash_token, verify_password
from .storage import ObjectStorage

__version__ = "0.1.0"

__all__ = [
    "ROLE_PERMISSIONS",
    "AppContext",
    "AppError",
    "BadRequestError",
    "Base",
    "BaseSchema",
    "BookFormat",
    "BookStatus",
    "Components",
    "ConflictError",
    "Currency",
    "CursorPage",
    "Database",
    "ErrorResponse",
    "Event",
    "EventConsumer",
    "EventPublisher",
    "EventType",
    "ForbiddenError",
    "JobStatus",
    "MessageResponse",
    "NotFoundError",
    "NotificationChannel",
    "ObjectStorage",
    "OrderStatus",
    "Page",
    "PageParams",
    "PaymentProvider",
    "PaymentRequiredError",
    "Permission",
    "Principal",
    "RateLimitedError",
    "ServiceSettings",
    "ServiceUnavailableError",
    "SoftDeleteMixin",
    "TimestampMixin",
    "UUIDPrimaryKeyMixin",
    "UnauthorizedError",
    "UnsupportedMediaTypeError",
    "UpstreamError",
    "UserRole",
    "ValidationError",
    "__version__",
    "apply_search",
    "apply_sorting",
    "configure_logging",
    "create_app",
    "decode_cursor",
    "encode_cursor",
    "generate_token",
    "get_logger",
    "hash_password",
    "hash_token",
    "page_params",
    "paginate",
    "run",
    "verify_password",
]
