import time

from app.core import auth


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
