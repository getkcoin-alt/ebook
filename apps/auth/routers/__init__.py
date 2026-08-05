"""HTTP routers for the authentication service."""

from routers.admin import audit_router
from routers.admin import router as admin_users_router
from routers.auth import router as auth_router
from routers.internal import router as internal_router
from routers.mfa import router as mfa_router
from routers.oauth import router as oauth_router
from routers.sessions import router as sessions_router

__all__ = [
    "admin_users_router",
    "audit_router",
    "auth_router",
    "internal_router",
    "mfa_router",
    "oauth_router",
    "sessions_router",
]
