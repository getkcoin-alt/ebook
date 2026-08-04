"""Shared fixtures for the core package tests."""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest
import pytest_asyncio
from asgi_lifespan import LifespanManager
from fastapi import APIRouter, FastAPI
from httpx import ASGITransport, AsyncClient

from knowledgeos_core import Components, ServiceSettings, create_app
from knowledgeos_core.deps import CurrentUser, OptionalUser


@pytest.fixture
def settings() -> ServiceSettings:
    return ServiceSettings(
        service_name="test-svc",
        service_version="9.9.9",
        environment="production",  # exercises the production code paths
        log_format="console",
    )


@pytest.fixture
def router() -> APIRouter:
    api = APIRouter(prefix="/v1")

    @api.get("/public")
    async def public() -> dict[str, bool]:
        return {"ok": True}

    @api.get("/private")
    async def private(user: CurrentUser) -> dict[str, str]:
        return {"user": user.user_id}

    @api.get("/maybe")
    async def maybe(user: OptionalUser) -> dict[str, str | None]:
        return {"user": user.user_id if user else None}

    @api.get("/boom")
    async def boom() -> None:
        raise RuntimeError("leaky detail postgres://user:pw@host/db")

    @api.post("/echo")
    async def echo(payload: dict[str, str]) -> dict[str, str]:
        return payload

    return api


@pytest.fixture
def app(settings: ServiceSettings, router: APIRouter) -> FastAPI:
    return create_app(settings=settings, components=Components(), routers=[router])


@pytest_asyncio.fixture
async def client(app: FastAPI) -> AsyncIterator[AsyncClient]:
    async with (
        LifespanManager(app),
        AsyncClient(
            # raise_app_exceptions=False so unhandled errors produce a response
            # instead of propagating — that is what a real ASGI server does.
            transport=ASGITransport(app=app, raise_app_exceptions=False),
            base_url="http://test",
        ) as http_client,
    ):
        yield http_client
