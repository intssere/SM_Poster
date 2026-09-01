from __future__ import annotations

import base64
import hashlib
import hmac
import json
import secrets
import time
from typing import Any

from fastapi import Request

from app.core.config import get_settings

SESSION_COOKIE = "ds_admin_session"
SESSION_TTL = 3600


def auth_configured() -> bool:
    settings = get_settings()
    return bool(settings.app_secret_key and len(settings.app_secret_key) >= 32 and settings.admin_username and settings.admin_password_hash)


def auth_is_bypassed() -> bool:
    settings = get_settings()
    return settings.auth_disabled and not settings.is_exposed


def _sign(payload: str) -> str:
    key = get_settings().app_secret_key.encode()
    digest = hmac.new(key, payload.encode(), hashlib.sha256).digest()
    return f"{payload}.{base64.urlsafe_b64encode(digest).decode().rstrip('=')}"


def make_session(username: str) -> str:
    body = base64.urlsafe_b64encode(json.dumps({"sub": username, "exp": int(time.time()) + get_settings().auth_session_ttl_seconds}, separators=(",", ":")).encode()).decode().rstrip("=")
    return _sign(body)


def verify_session(token: str | None) -> str | None:
    if not token or "." not in token or not auth_configured():
        return None
    body, signature = token.rsplit(".", 1)
    expected = _sign(body).rsplit(".", 1)[1]
    if not hmac.compare_digest(signature, expected):
        return None
    try:
        padding = "=" * (-len(body) % 4)
        payload: dict[str, Any] = json.loads(base64.urlsafe_b64decode(body + padding))
        if int(payload.get("exp", 0)) < int(time.time()) or payload.get("sub") != get_settings().admin_username:
            return None
        return str(payload["sub"])
    except (ValueError, TypeError, json.JSONDecodeError):
        return None


def password_matches(password: str) -> bool:
    encoded = get_settings().admin_password_hash or ""
    try:
        scheme, iterations, salt, digest = encoded.split("$", 3)
        if scheme != "pbkdf2_sha256":
            return False
        calculated = hashlib.pbkdf2_hmac("sha256", password.encode(), base64.urlsafe_b64decode(salt + "=="), int(iterations))
        return hmac.compare_digest(base64.urlsafe_b64encode(calculated).decode().rstrip("="), digest)
    except (ValueError, TypeError):
        return False


def hash_password(password: str, *, iterations: int = 310_000) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode(), salt, iterations)
    return f"pbkdf2_sha256${iterations}${base64.urlsafe_b64encode(salt).decode().rstrip('=')}${base64.urlsafe_b64encode(digest).decode().rstrip('=')}"


def current_user(request: Request) -> str | None:
    if auth_is_bypassed():
        return "local-test-admin"
    return verify_session(request.cookies.get(SESSION_COOKIE))
