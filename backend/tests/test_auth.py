import time

import pytest
from fastapi.testclient import TestClient
import hashlib
from types import SimpleNamespace
from contextlib import nullcontext
from app.db.session import get_db
from app.api.routes import proposals as proposals_routes

from app.services.public_creative_media import verified_png

from app.core import auth


@pytest.fixture
def auth_client(monkeypatch):
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "s" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", "http://localhost:5000")
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.delenv("REPLIT_DEV_DOMAIN", raising=False)
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.main import app
    return TestClient(app)


def test_api_auth_integration_and_cookie_security(auth_client):
    client = auth_client
    anonymous = client.get("/api/utilities/product-score", headers={"Origin": "http://localhost:5000"})
    assert anonymous.status_code == 401
    assert anonymous.headers["access-control-allow-origin"] == "http://localhost:5000"
    invalid = client.post("/api/auth/login", json={"username": "nope", "password": "wrong"}, headers={"Origin": "http://localhost:5000"})
    assert invalid.status_code == 401
    assert invalid.headers["access-control-allow-origin"] == "http://localhost:5000"
    response = client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    assert response.status_code == 200
    cookie = response.headers["set-cookie"].lower()
    assert "httponly" in cookie and "samesite=strict" in cookie
    assert client.get("/api/utilities/product-score").status_code != 401
    assert client.get("/api/auth/status").json()["authenticated"] is True


def test_verified_png_returns_exact_bytes_and_rejects_tampering(tmp_path):
    payload = b"\x89PNG\r\n\x1a\nverified-pixels"
    digest = hashlib.sha256(payload).hexdigest()
    creative = SimpleNamespace(id="creative-1", sha256=digest, render_status="RENDERED")
    (tmp_path / "creative-1.png").write_bytes(payload)
    assert verified_png(creative, digest, root=tmp_path) == payload
    (tmp_path / "creative-1.png").write_bytes(payload + b"tampered")
    assert verified_png(creative, digest, root=tmp_path) is None


def test_public_creative_route_is_exact_anonymous_get_head_boundary(auth_client, monkeypatch, tmp_path):
    payload = b"\x89PNG\r\n\x1a\nroute-test-pixels"
    digest = hashlib.sha256(payload).hexdigest()
    creative = SimpleNamespace(id="creative-route", sha256=digest, render_status="RENDERED")
    (tmp_path / "creative-route.png").write_bytes(payload)

    class FakeDB:
        no_autoflush = nullcontext()
        def get(self, model, row_id):
            return creative if row_id == creative.id else None

    from app.main import app
    app.dependency_overrides[get_db] = lambda: FakeDB()
    monkeypatch.setattr(proposals_routes, "verified_png", lambda row, requested_digest: verified_png(row, requested_digest, root=tmp_path))
    try:
        client = auth_client
        url = f"/api/pins/public-creatives/{creative.id}/{digest}.png"
        response = client.get(url)
        assert response.status_code == 200 and response.content == payload
        assert response.headers["content-type"] == "image/png"
        assert int(response.headers["content-length"]) == len(payload)
        assert all(v in response.headers["cache-control"] for v in ("public", "max-age=31536000", "immutable"))
        assert response.headers["x-content-type-options"] == "nosniff"
        head = client.head(url)
        assert head.status_code == 200 and head.content == b""
        assert int(head.headers["content-length"]) == len(payload)
        assert client.get(f"/api/pins/public-creatives/{creative.id}/{'b' * 64}.png").status_code == 404
        assert client.get(f"/api/pins/public-creatives/other/{digest}.png").status_code == 404
        creative.render_status = "PENDING"
        assert client.get(url).status_code == 404
        creative.render_status = "RENDERED"
        (tmp_path / "creative-route.png").write_bytes(payload + b"tampered")
        assert client.get(url).status_code == 404
        (tmp_path / "creative-route.png").write_bytes(payload)
        assert client.get("/api/pins/summary").status_code == 401
        assert client.post(url, headers={"Origin": "http://localhost:5000"}).status_code == 401
    finally:
        app.dependency_overrides.pop(get_db, None)


def test_exposed_cookie_and_replit_origin(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "p" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("REPLIT_DEV_DOMAIN", "studio.example.replit.dev")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.main import app
    client = TestClient(app)
    response = client.post("/api/auth/login", json={"username": "admin", "password": "secret"}, headers={"Origin": "https://studio.example.replit.dev"})
    assert response.status_code == 200
    assert "secure" in response.headers["set-cookie"].lower()
    assert client.get("/api/auth/status", headers={"Origin": "https://studio.example.replit.dev"}).status_code == 200


def test_session_tamper_expiry_logout_and_origin_controls(auth_client, monkeypatch):
    client = auth_client
    login = client.post("/api/auth/login", json={"username": "admin", "password": "secret"})
    token = login.cookies.get(auth.SESSION_COOKIE)
    assert client.get("/api/utilities/product-score", cookies={auth.SESSION_COOKIE: token + "x"}).status_code == 401
    assert client.post("/api/utilities/product-score", json={"inventory_available": True}).status_code == 403
    assert client.post("/api/utilities/product-score", json={"inventory_available": True}, headers={"Origin": "https://evil.example"}).status_code == 403
    assert client.post("/api/utilities/product-score", json={"inventory_available": True}, headers={"Origin": "http://localhost:5000"}).status_code != 403
    logout = client.post("/api/auth/logout", headers={"Origin": "http://localhost:5000"})
    assert logout.status_code == 200
    deleted_cookie = logout.headers["set-cookie"].lower()
    assert auth.SESSION_COOKIE in deleted_cookie and ("expires=" in deleted_cookie or "max-age=0" in deleted_cookie)
    assert client.get("/api/utilities/product-score").status_code == 401
    monkeypatch.setenv("AUTH_SESSION_TTL_SECONDS", "-1")
    from app.core.config import get_settings
    get_settings.cache_clear()
    assert client.post("/api/auth/login", json={"username": "admin", "password": "secret"}).status_code == 200
    assert client.get("/api/utilities/product-score").status_code == 401


@pytest.mark.parametrize("env_name", ["APP_ENV", "REPLIT_DEPLOYMENT", "REPLIT_DEV_DOMAIN"])
def test_auth_disabled_never_bypasses_exposed_modes(monkeypatch, env_name):
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 48)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("REPLIT_DEPLOYMENT", raising=False)
    monkeypatch.delenv("REPLIT_DEV_DOMAIN", raising=False)
    monkeypatch.setenv(env_name, "production" if env_name == "APP_ENV" else "preview.example")
    from app.core.config import get_settings
    get_settings.cache_clear()
    from app.main import app
    assert TestClient(app).get("/api/utilities/product-score").status_code == 503


def test_health_and_auth_status_are_public(auth_client):
    client = auth_client
    assert client.get("/api/health").status_code == 200
    assert client.get("/api/auth/status").status_code == 200


def test_cors_preflight_and_disallowed_origin(auth_client):
    client = auth_client
    preflight = client.options("/api/utilities/product-score", headers={"Origin": "http://localhost:5000", "Access-Control-Request-Method": "POST"})
    assert preflight.status_code == 200
    assert preflight.headers["access-control-allow-origin"] == "http://localhost:5000"
    assert "POST" in preflight.headers["access-control-allow-methods"]
    denied = client.get("/api/utilities/product-score", headers={"Origin": "https://evil.example"})
    assert denied.status_code == 403
    assert "access-control-allow-origin" not in denied.headers


def test_signed_session_round_trip_and_tamper_rejection(monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 40)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    from app.core.config import get_settings
    get_settings.cache_clear()
    token = auth.make_session("admin")
    assert auth.verify_session(token) == "admin"
    assert auth.verify_session(token + "tampered") is None


def test_expired_session_rejected(monkeypatch):
    monkeypatch.setenv("APP_SECRET_KEY", "y" * 40)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("secret"))
    monkeypatch.setenv("AUTH_SESSION_TTL_SECONDS", "-1")
    from app.core.config import get_settings
    get_settings.cache_clear()
    assert auth.verify_session(auth.make_session("admin")) is None
    monkeypatch.delenv("AUTH_SESSION_TTL_SECONDS", raising=False)
    get_settings.cache_clear()


def test_password_hash_is_not_reversible_or_logged(monkeypatch):
    encoded = auth.hash_password("secret")
    assert "secret" not in encoded
    monkeypatch.setenv("APP_SECRET_KEY", "z" * 40)
    monkeypatch.setenv("ADMIN_USERNAME", "admin")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", encoded)
    from app.core.config import get_settings
    get_settings.cache_clear()
    assert auth.password_matches("secret")
    assert not auth.password_matches("wrong")


def test_auth_bypass_is_never_active_in_exposed_mode(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    monkeypatch.setenv("APP_SECRET_KEY", "x" * 40)
    from app.core.config import get_settings
    get_settings.cache_clear()
    assert not auth.auth_is_bypassed()
    monkeypatch.setenv("APP_ENV", "development")
    monkeypatch.setenv("AUTH_DISABLED", "true")
    get_settings.cache_clear()
