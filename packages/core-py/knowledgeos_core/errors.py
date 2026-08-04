"""Platform error model.

Every service returns the same JSON error envelope, so the gateway, the SDK and the
frontend only ever parse one shape::

    {
      "error": {
        "code": "resource_not_found",
        "message": "Book not found",
        "details": {"book_id": "..."},
        "request_id": "01J..."
      }
    }

``code`` is a stable machine-readable string. ``message`` is human-facing and safe to
surface. Unexpected exceptions never leak their text in production.
"""

from __future__ import annotations

from typing import Any

from fastapi import FastAPI, Request, status
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.exceptions import HTTPException as StarletteHTTPException

from .logging import get_logger, request_id_ctx

logger = get_logger(__name__)


class AppError(Exception):
    """Base class for all deliberate, expected failures."""

    status_code: int = status.HTTP_500_INTERNAL_SERVER_ERROR
    code: str = "internal_error"
    message: str = "An unexpected error occurred."

    def __init__(
        self,
        message: str | None = None,
        *,
        code: str | None = None,
        details: dict[str, Any] | None = None,
        status_code: int | None = None,
        headers: dict[str, str] | None = None,
    ) -> None:
        self.message = message or self.message
        self.code = code or self.code
        self.details = details or {}
        self.status_code = status_code or self.status_code
        self.headers = headers or {}
        super().__init__(self.message)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"code": self.code, "message": self.message}
        if self.details:
            payload["details"] = self.details
        if (rid := request_id_ctx.get()) is not None:
            payload["request_id"] = rid
        return {"error": payload}


class BadRequestError(AppError):
    status_code = status.HTTP_400_BAD_REQUEST
    code = "bad_request"
    message = "The request could not be processed."


class ValidationError(AppError):
    # Literal codes for 422/413: Starlette has renamed these constants across
    # versions (UNPROCESSABLE_ENTITY -> UNPROCESSABLE_CONTENT), and the number is
    # the part of the contract that never changes.
    status_code = 422
    code = "validation_error"
    message = "The request payload failed validation."


class UnauthorizedError(AppError):
    status_code = status.HTTP_401_UNAUTHORIZED
    code = "unauthorized"
    message = "Authentication is required."

    def __init__(self, message: str | None = None, **kwargs: Any) -> None:
        kwargs.setdefault("headers", {"WWW-Authenticate": "Bearer"})
        super().__init__(message, **kwargs)


class ForbiddenError(AppError):
    status_code = status.HTTP_403_FORBIDDEN
    code = "forbidden"
    message = "You do not have permission to perform this action."


class NotFoundError(AppError):
    status_code = status.HTTP_404_NOT_FOUND
    code = "resource_not_found"
    message = "The requested resource does not exist."


class ConflictError(AppError):
    status_code = status.HTTP_409_CONFLICT
    code = "conflict"
    message = "The request conflicts with the current state of the resource."


class PayloadTooLargeError(AppError):
    status_code = 413
    code = "payload_too_large"
    message = "The request body is too large."


class UnsupportedMediaTypeError(AppError):
    status_code = status.HTTP_415_UNSUPPORTED_MEDIA_TYPE
    code = "unsupported_media_type"
    message = "The uploaded file type is not supported."


class RateLimitedError(AppError):
    status_code = status.HTTP_429_TOO_MANY_REQUESTS
    code = "rate_limited"
    message = "Too many requests. Please slow down."

    def __init__(self, message: str | None = None, *, retry_after: int = 60, **kwargs: Any):
        headers = kwargs.pop("headers", {})
        headers.setdefault("Retry-After", str(retry_after))
        super().__init__(message, headers=headers, **kwargs)


class PaymentRequiredError(AppError):
    status_code = status.HTTP_402_PAYMENT_REQUIRED
    code = "payment_required"
    message = "This resource requires an active purchase or subscription."


class ServiceUnavailableError(AppError):
    status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    code = "service_unavailable"
    message = "A dependency is temporarily unavailable."


class UpstreamError(AppError):
    """A downstream service or third-party API failed."""

    status_code = status.HTTP_502_BAD_GATEWAY
    code = "upstream_error"
    message = "An upstream service returned an error."


def install_exception_handlers(app: FastAPI) -> None:
    """Register the platform-wide handlers on a FastAPI app."""

    @app.exception_handler(AppError)
    async def _app_error(_request: Request, exc: AppError) -> JSONResponse:
        # 5xx are unexpected even when deliberate; log them loudly.
        log = logger.error if exc.status_code >= 500 else logger.info
        log(
            "request.failed",
            error_code=exc.code,
            status_code=exc.status_code,
            detail=exc.message,
        )
        return JSONResponse(status_code=exc.status_code, content=exc.to_dict(), headers=exc.headers)

    @app.exception_handler(RequestValidationError)
    async def _validation(_request: Request, exc: RequestValidationError) -> JSONResponse:
        err = ValidationError(details={"fields": jsonable_encoder(exc.errors())})
        logger.info("request.validation_failed", errors=err.details)
        return JSONResponse(status_code=err.status_code, content=err.to_dict())

    @app.exception_handler(StarletteHTTPException)
    async def _http(_request: Request, exc: StarletteHTTPException) -> JSONResponse:
        mapped = AppError(
            str(exc.detail),
            code=_CODE_BY_STATUS.get(exc.status_code, "http_error"),
            status_code=exc.status_code,
            headers=dict(exc.headers or {}),
        )
        return JSONResponse(
            status_code=mapped.status_code, content=mapped.to_dict(), headers=mapped.headers
        )

    @app.exception_handler(Exception)
    async def _unhandled(request: Request, exc: Exception) -> JSONResponse:
        logger.exception(
            "request.unhandled_exception",
            path=request.url.path,
            method=request.method,
            exc_type=type(exc).__name__,
        )
        # Never echo an unhandled exception's text to a client — it can carry
        # connection strings, file paths or row contents.
        return JSONResponse(status_code=500, content=AppError().to_dict())


_CODE_BY_STATUS: dict[int, str] = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "resource_not_found",
    405: "method_not_allowed",
    409: "conflict",
    413: "payload_too_large",
    415: "unsupported_media_type",
    422: "validation_error",
    429: "rate_limited",
    500: "internal_error",
    502: "upstream_error",
    503: "service_unavailable",
    504: "upstream_timeout",
}
