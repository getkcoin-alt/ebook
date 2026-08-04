"""S3-compatible object storage (MinIO locally, any S3 provider in production).

**Files never pass through the API.** Uploads go browser -> MinIO via a presigned
PUT; downloads go browser -> MinIO via a presigned GET. Proxying a 40MB PDF through
FastAPI would occupy a worker for the whole transfer and make the API's memory
profile a function of file size.

**Paid content is never public.** Book files live under ``private/`` and are only
reachable through a short-lived signed URL minted after an entitlement check.
Covers and thumbnails live under ``public/`` and are cacheable by a CDN.
"""

from __future__ import annotations

import asyncio
import mimetypes
import re
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from functools import partial
from typing import Any, Literal

import boto3
from botocore.client import Config
from botocore.exceptions import BotoCoreError, ClientError

from .config import ServiceSettings
from .errors import NotFoundError, UnsupportedMediaTypeError, UpstreamError
from .logging import get_logger

logger = get_logger(__name__)

Visibility = Literal["public", "private"]

#: Extension -> MIME allowlist. Content type is validated against this rather than
#: trusted from the client, which is how "invoice.pdf.exe" gets stopped.
ALLOWED_UPLOAD_TYPES: dict[str, set[str]] = {
    "book": {"application/pdf", "application/epub+zip", "application/x-mobipocket-ebook"},
    "cover": {"image/jpeg", "image/png", "image/webp", "image/avif"},
    "avatar": {"image/jpeg", "image/png", "image/webp"},
    "audio": {"audio/mpeg", "audio/mp4", "audio/aac", "audio/ogg"},
    "attachment": {"application/pdf", "application/zip", "text/plain"},
}

_SAFE_NAME = re.compile(r"[^A-Za-z0-9._-]+")


def sanitize_filename(filename: str, *, max_length: int = 120) -> str:
    """Strip path traversal and hostile characters from a user-supplied name.

    Keys are generated server-side, so this only affects the human-readable suffix,
    but a name containing ``../`` or a newline can still poison logs and
    ``Content-Disposition`` headers.
    """
    name = filename.replace("\\", "/").split("/")[-1]
    name = _SAFE_NAME.sub("-", name).strip(".-") or "file"
    if len(name) > max_length:
        stem, _, ext = name.rpartition(".")
        name = f"{stem[: max_length - len(ext) - 1]}.{ext}" if ext else name[:max_length]
    return name


@dataclass(slots=True)
class UploadTarget:
    """A presigned upload the browser can PUT to directly."""

    url: str
    key: str
    fields: dict[str, str]
    expires_in: int
    max_bytes: int


@dataclass(slots=True)
class StoredObject:
    key: str
    size: int
    etag: str
    content_type: str
    last_modified: datetime


class ObjectStorage:
    """Async facade over boto3.

    boto3 is synchronous, so every call is dispatched to the default thread pool.
    That keeps the event loop free without pulling in a second AWS SDK.
    """

    def __init__(self, settings: ServiceSettings) -> None:
        self._settings = settings
        self._bucket = settings.s3_bucket
        self._client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint_url,
            aws_access_key_id=settings.s3_access_key,
            aws_secret_access_key=settings.s3_secret_key,
            region_name=settings.s3_region,
            use_ssl=settings.s3_use_ssl,
            config=Config(
                signature_version="s3v4",
                # MinIO requires path-style addressing; virtual-host style needs
                # wildcard DNS that a self-hosted deployment will not have.
                s3={"addressing_style": "path"},
                retries={"max_attempts": 3, "mode": "adaptive"},
                connect_timeout=5,
                read_timeout=30,
            ),
        )
        #: Public endpoint differs from the internal one on Railway: the API signs
        #: with the private hostname but the browser must be handed the public one.
        self._public_endpoint = settings.s3_public_endpoint_url or settings.s3_endpoint_url

    async def _call(self, method: str, **kwargs: Any) -> Any:
        loop = asyncio.get_running_loop()
        func = partial(getattr(self._client, method), **kwargs)
        try:
            return await loop.run_in_executor(None, func)
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code in {"404", "NoSuchKey", "NotFound"}:
                raise NotFoundError("The requested file does not exist.") from exc
            logger.error("storage.client_error", method=method, code=code)
            raise UpstreamError("Object storage returned an error.") from exc
        except BotoCoreError as exc:
            logger.error("storage.transport_error", method=method, error=str(exc))
            raise UpstreamError("Object storage is unreachable.") from exc

    # ---- key layout -----------------------------------------------------

    @staticmethod
    def build_key(
        *,
        visibility: Visibility,
        category: str,
        owner_id: str,
        filename: str,
        version: str | None = None,
    ) -> str:
        """``<visibility>/<category>/<owner>/<yyyy>/<mm>/<uuid>-<name>``.

        Date segments keep any single prefix from growing unbounded, which matters
        for listing performance and for lifecycle rules that expire old temp files.
        """
        now = datetime.now(UTC)
        safe = sanitize_filename(filename)
        unique = version or uuid.uuid4().hex[:12]
        return f"{visibility}/{category}/{owner_id}/{now:%Y/%m}/{unique}-{safe}"

    # ---- uploads --------------------------------------------------------

    def validate_content_type(self, category: str, content_type: str) -> None:
        allowed = ALLOWED_UPLOAD_TYPES.get(category)
        if allowed is None:
            raise UnsupportedMediaTypeError(f"Unknown upload category '{category}'.")
        if content_type not in allowed:
            raise UnsupportedMediaTypeError(
                f"'{content_type}' is not accepted for {category} uploads.",
                details={"allowed": sorted(allowed)},
            )

    async def create_upload_target(
        self,
        *,
        category: str,
        owner_id: str,
        filename: str,
        content_type: str,
        visibility: Visibility = "private",
        max_bytes: int = 100 * 1024 * 1024,
        expires_in: int | None = None,
    ) -> UploadTarget:
        """Mint a presigned POST for a direct browser upload.

        POST (not PUT) because the policy document can enforce a maximum size and an
        exact content type *server-side*. With a presigned PUT the client can send a
        200MB file to a URL we issued for a 5MB avatar.
        """
        self.validate_content_type(category, content_type)
        ttl = expires_in or self._settings.s3_signature_ttl
        key = self.build_key(
            visibility=visibility, category=category, owner_id=owner_id, filename=filename
        )
        presigned = await self._call(
            "generate_presigned_post",
            Bucket=self._bucket,
            Key=key,
            Fields={"Content-Type": content_type},
            Conditions=[
                {"Content-Type": content_type},
                ["content-length-range", 1, max_bytes],
            ],
            ExpiresIn=ttl,
        )
        logger.info("storage.upload_target_created", key=key, category=category)
        return UploadTarget(
            url=self._rewrite_public(presigned["url"]),
            key=key,
            fields=presigned["fields"],
            expires_in=ttl,
            max_bytes=max_bytes,
        )

    async def put_bytes(
        self, key: str, data: bytes, *, content_type: str | None = None, cache_seconds: int = 0
    ) -> StoredObject:
        """Server-side upload, for artefacts the automation pipeline generates."""
        guessed = content_type or mimetypes.guess_type(key)[0] or "application/octet-stream"
        extra: dict[str, Any] = {"ContentType": guessed}
        if cache_seconds:
            extra["CacheControl"] = f"public, max-age={cache_seconds}, immutable"
        response = await self._call("put_object", Bucket=self._bucket, Key=key, Body=data, **extra)
        return StoredObject(
            key=key,
            size=len(data),
            etag=response.get("ETag", "").strip('"'),
            content_type=guessed,
            last_modified=datetime.now(UTC),
        )

    # ---- downloads ------------------------------------------------------

    async def signed_download_url(
        self,
        key: str,
        *,
        expires_in: int | None = None,
        download_filename: str | None = None,
    ) -> str:
        """Short-lived GET URL.

        Callers MUST verify entitlement before calling this — the URL itself is the
        capability, and anyone holding it can read the object until it expires. Keep
        the TTL short (minutes) so a leaked link in a shared screenshot goes stale.
        """
        params: dict[str, Any] = {"Bucket": self._bucket, "Key": key}
        if download_filename:
            safe = sanitize_filename(download_filename)
            params["ResponseContentDisposition"] = f'attachment; filename="{safe}"'
        url = await self._call(
            "generate_presigned_url",
            ClientMethod="get_object",
            Params=params,
            ExpiresIn=expires_in or self._settings.s3_signature_ttl,
        )
        return self._rewrite_public(url)

    def public_url(self, key: str) -> str:
        """Permanent URL for objects under ``public/`` (covers, thumbnails)."""
        base = (self._public_endpoint or "").rstrip("/")
        return f"{base}/{self._bucket}/{key}"

    def _rewrite_public(self, url: str) -> str:
        """Swap the internal endpoint for the browser-reachable one.

        The signature covers the path and query, not the host, so replacing the
        hostname keeps the presigned URL valid.
        """
        internal = self._settings.s3_endpoint_url
        if internal and self._public_endpoint and internal != self._public_endpoint:
            return url.replace(internal.rstrip("/"), self._public_endpoint.rstrip("/"), 1)
        return url

    # ---- object management ---------------------------------------------

    async def head(self, key: str) -> StoredObject:
        response = await self._call("head_object", Bucket=self._bucket, Key=key)
        return StoredObject(
            key=key,
            size=int(response["ContentLength"]),
            etag=response.get("ETag", "").strip('"'),
            content_type=response.get("ContentType", "application/octet-stream"),
            last_modified=response["LastModified"],
        )

    async def exists(self, key: str) -> bool:
        try:
            await self.head(key)
            return True
        except NotFoundError:
            return False

    async def get_bytes(self, key: str) -> bytes:
        response = await self._call("get_object", Bucket=self._bucket, Key=key)
        return await asyncio.get_running_loop().run_in_executor(None, response["Body"].read)

    async def delete(self, key: str) -> None:
        await self._call("delete_object", Bucket=self._bucket, Key=key)
        logger.info("storage.deleted", key=key)

    async def copy(self, source_key: str, dest_key: str) -> None:
        await self._call(
            "copy_object",
            Bucket=self._bucket,
            CopySource={"Bucket": self._bucket, "Key": source_key},
            Key=dest_key,
        )

    # ---- bootstrap & health ---------------------------------------------

    async def ensure_bucket(self) -> None:
        """Create the bucket on first boot and mark the public prefix readable."""
        try:
            await self._call("head_bucket", Bucket=self._bucket)
        except (NotFoundError, UpstreamError):
            try:
                await self._call("create_bucket", Bucket=self._bucket)
                logger.info("storage.bucket_created", bucket=self._bucket)
            except UpstreamError:
                logger.warning("storage.bucket_create_failed", bucket=self._bucket)
                return

        policy = (
            '{"Version":"2012-10-17","Statement":[{"Effect":"Allow",'
            '"Principal":{"AWS":["*"]},"Action":["s3:GetObject"],'
            f'"Resource":["arn:aws:s3:::{self._bucket}/public/*"]}}]}}'
        )
        try:
            await self._call("put_bucket_policy", Bucket=self._bucket, Policy=policy)
        except UpstreamError:
            # Managed S3 accounts often forbid policy writes; covers are then served
            # through the CDN instead. Not fatal.
            logger.info("storage.public_policy_skipped", bucket=self._bucket)

    async def healthcheck(self) -> dict[str, Any]:
        try:
            await self._call("head_bucket", Bucket=self._bucket)
            return {"status": "up", "bucket": self._bucket}
        except Exception as exc:
            return {"status": "down", "error": str(exc)}
