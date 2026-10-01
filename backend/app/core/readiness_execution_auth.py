"""Purpose-separated, short-lived management grants; never business permits."""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import secrets
import time

from fastapi import HTTPException, Request

from app.core import auth
from app.core.config import Settings, get_settings
from app.services.readiness_execution_contract import (
    OPERATION, ReadinessBinding, ReadinessError,
)

CONFIRMATION = "EXECUTE-ONE-OBJECT-STORAGE-READINESS-PROBE-V1"
GRANT_HEADER = "X-Readiness-Authorization"
CONFIRM_HEADER = "X-Readiness-Confirmation"
MAX_GRANT_TTL = 180
_DOMAIN = b"management-object-storage-readiness-v1\x00"
_SHA = re.compile(r"[0-9a-f]{40}\Z")
_DIGEST = re.compile(r"[0-9a-f]{64}\Z")
_ENCODED = re.compile(r"[A-Za-z0-9_-]+\Z")
_FIELDS = {"v", "purpose", "binding", "actor", "iat", "exp", "jti"}
NO_STORE = {"Cache-Control": "no-store"}


def binding_from_settings(settings: Settings) -> ReadinessBinding:
    binding = ReadinessBinding(
        canonical_commit_sha=settings.readiness_expected_canonical_commit_sha,
        canonical_tree_sha=settings.readiness_expected_canonical_tree_sha,
        release_commit_sha=settings.readiness_expected_release_commit_sha,
        release_tree_sha=settings.readiness_expected_release_tree_sha,
        overlay_sha256=settings.readiness_expected_overlay_sha256,
        probe_sha256=settings.readiness_expected_probe_sha256,
        topology=settings.readiness_expected_topology,
    )
    shas = (binding.canonical_commit_sha, binding.canonical_tree_sha,
            binding.release_commit_sha, binding.release_tree_sha)
    if (
        not all(_SHA.fullmatch(value) for value in shas)
        or not _DIGEST.fullmatch(binding.overlay_sha256)
        or not _DIGEST.fullmatch(binding.probe_sha256)
        or binding.topology not in {
            "canonical_with_worktree_overlay", "canonical_parent_with_checkpoint_overlay",
        }
        or (
            binding.topology == "canonical_with_worktree_overlay"
            and (binding.canonical_commit_sha != binding.release_commit_sha
                 or binding.canonical_tree_sha != binding.release_tree_sha)
        )
    ):
        raise ReadinessError("RELEASE_BINDING_NOT_CONFIGURED")
    return binding


def actor_digest(username: str) -> str:
    return hashlib.sha256(username.encode()).hexdigest()


def _sign(body: str, settings: Settings) -> str:
    if len(settings.app_secret_key) < 32:
        raise ReadinessError("AUTHENTICATION_NOT_CONFIGURED")
    return base64.urlsafe_b64encode(hmac.new(
        settings.app_secret_key.encode(), _DOMAIN + body.encode(), hashlib.sha256,
    ).digest()).decode().rstrip("=")


def issue_authorization(
    settings: Settings, binding: ReadinessBinding, *, now: int | None = None,
) -> str:
    """Offline issuance helper, not an HTTP endpoint or an execution path.

    A future authorized operator must approve/configure the exact new release.
    This engineering task never calls the helper with workspace credentials.
    """
    if binding != binding_from_settings(settings) or not settings.admin_username:
        raise ReadinessError("RELEASE_BINDING_NOT_CONFIGURED")
    issued = int(time.time()) if now is None else now
    payload = {
        "v": 1, "purpose": OPERATION, "binding": binding.digest,
        "actor": actor_digest(settings.admin_username),
        "iat": issued, "exp": issued + MAX_GRANT_TTL, "jti": secrets.token_hex(32),
    }
    body = base64.urlsafe_b64encode(json.dumps(
        payload, sort_keys=True, separators=(",", ":"),
    ).encode()).decode().rstrip("=")
    return f"{body}.{_sign(body, settings)}"


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError("duplicate field")
        result[key] = value
    return result


def verify_authorization(
    token: str, settings: Settings, binding: ReadinessBinding, username: str,
    *, now: int | None = None,
) -> dict:
    try:
        if not isinstance(token, str) or len(token) > 2048 or token.count(".") != 1:
            raise ValueError()
        body, signature = token.split(".")
        if not _ENCODED.fullmatch(body) or not _ENCODED.fullmatch(signature):
            raise ValueError()
        if not hmac.compare_digest(signature, _sign(body, settings)):
            raise ValueError()
        claims = json.loads(
            base64.urlsafe_b64decode(body + "=" * (-len(body) % 4)),
            object_pairs_hook=_unique_object,
        )
        current = int(time.time()) if now is None else now
        if (
            not isinstance(claims, dict) or set(claims) != _FIELDS
            or type(claims["v"]) is not int or claims["v"] != 1
            or claims["purpose"] != OPERATION
            or claims["binding"] != binding.digest
            or claims["actor"] != actor_digest(username)
            or type(claims["iat"]) is not int or type(claims["exp"]) is not int
            or not claims["iat"] <= current < claims["exp"]
            or not 0 < claims["exp"] - claims["iat"] <= MAX_GRANT_TTL
            or not isinstance(claims["jti"], str) or not _DIGEST.fullmatch(claims["jti"])
        ):
            raise ValueError()
        return claims
    except (ValueError, TypeError, KeyError, UnicodeError, RecursionError):
        raise ReadinessError("EXECUTION_AUTHORIZATION_INVALID", 403) from None


def require_real_admin(request: Request) -> str:
    """Independent of middleware: bypass is never sufficient for management."""
    settings = get_settings()
    if settings.auth_disabled or not auth.auth_configured():
        raise HTTPException(503, detail={"code": "REAL_ADMIN_AUTH_REQUIRED"}, headers=NO_STORE)
    try:
        username = auth.verify_session(request.cookies.get(auth.SESSION_COOKIE))
    except (ValueError, TypeError, UnicodeError, AttributeError, RecursionError):
        username = None
    if not username:
        raise HTTPException(401, detail={"code": "AUTHENTICATION_REQUIRED"}, headers=NO_STORE)
    origins = request.headers.getlist("origin")
    if len(origins) != 1 or origins[0].rstrip("/") not in settings.allowed_origins:
        raise HTTPException(403, detail={"code": "ORIGIN_REQUIRED"}, headers=NO_STORE)
    return username