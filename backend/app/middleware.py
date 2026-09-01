from __future__ import annotations

from starlette.middleware.base import BaseHTTPMiddleware
from starlette.responses import JSONResponse

from app.core.auth import auth_configured, auth_is_bypassed, current_user
from app.core.config import get_settings


PUBLIC_PATHS = {"/api/health", "/api/auth/login", "/api/auth/status", "/api/channels/pinterest/callback"}


class AdminAuthMiddleware(BaseHTTPMiddleware):
    async def dispatch(self, request, call_next):
        path = request.url.path
        if path.startswith("/api/"):
            settings = get_settings()
            if request.method == "OPTIONS":
                return await call_next(request)
            origin = request.headers.get("origin")
            if origin and origin.rstrip("/") not in settings.allowed_origins:
                return JSONResponse({"detail": "Origin is not allowed."}, status_code=403)
            if request.method not in {"GET", "HEAD", "OPTIONS"} and origin is None and path not in {"/api/auth/login"}:
                return JSONResponse({"detail": "Origin header is required."}, status_code=403)
            if path not in PUBLIC_PATHS:
                if settings.is_exposed and not auth_configured():
                    return JSONResponse({"detail": "Authentication is not configured."}, status_code=503)
                if not auth_is_bypassed() and not current_user(request):
                    return JSONResponse({"detail": "Authentication required."}, status_code=401)
        return await call_next(request)
