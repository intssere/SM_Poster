from types import SimpleNamespace

from fastapi.testclient import TestClient

from app.api.routes import buffer_reconciliation as route
from app.core import auth
from app.core.config import get_settings
from app.db.session import get_db
from app.main import app


PUBLICATION_ID = "transport-contract-publication"
ALLOWED_ORIGIN = "https://operator.example.test"


def _client(monkeypatch):
    monkeypatch.setenv("APP_ENV", "production")
    monkeypatch.setenv("AUTH_DISABLED", "false")
    monkeypatch.setenv("APP_SECRET_KEY", "t" * 48)
    monkeypatch.setenv("ADMIN_USERNAME", "operator")
    monkeypatch.setenv("ADMIN_PASSWORD_HASH", auth.hash_password("unused"))
    monkeypatch.setenv("AUTH_ALLOWED_ORIGINS", ALLOWED_ORIGIN)
    monkeypatch.delenv("REPLIT_DEV_DOMAIN", raising=False)
    get_settings.cache_clear()
    client = TestClient(app)
    client.cookies.set(auth.SESSION_COOKIE, auth.make_session("operator"))
    app.dependency_overrides[get_db] = lambda: SimpleNamespace()
    return client


def _payload():
    return {
        "confirmed": True,
        "confirmation_text_version": route.BUFFER_RECONCILIATION_CONFIRMATION_VERSION,
    }


def test_preflight_attests_safe_transport_contract(monkeypatch):
    client = _client(monkeypatch)
    monkeypatch.setattr(
        route,
        "attest_buffer_reconciliation_preflight",
        lambda db, publication_id: {
            "eligible": True,
            "pre_provider_eligible": True,
            "provider_free": True,
            "read_only": True,
            "reconciliation_performed": False,
            "code": None,
            "stage": None,
            "field": None,
        },
    )
    try:
        response = client.get(
            f"/api/publications/{PUBLICATION_ID}/reconcile-buffer/preflight"
        )
        assert response.status_code == 200
        body = response.json()
        assert body["provider_free"] is True
        assert body["read_only"] is True
        assert body["reconciliation_performed"] is False
        assert body["transport_contract"] == {
            "origin_required": True,
            "authenticated_session_required": True,
            "confirmation_required": True,
            "confirmation_text_version": "BUFFER_RECONCILIATION_V1",
        }
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()


def test_reconciliation_post_missing_or_disallowed_origin_fails_before_service(monkeypatch):
    client = _client(monkeypatch)
    calls = []

    async def forbidden_reconcile(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("reconciliation service must not run")

    monkeypatch.setattr(route, "reconcile_buffer", forbidden_reconcile)
    try:
        missing = client.post(
            f"/api/publications/{PUBLICATION_ID}/reconcile-buffer",
            json=_payload(),
        )
        assert missing.status_code == 403
        assert missing.json() == {"detail": "Origin header is required."}

        disallowed = client.post(
            f"/api/publications/{PUBLICATION_ID}/reconcile-buffer",
            headers={"Origin": "https://evil.example"},
            json=_payload(),
        )
        assert disallowed.status_code == 403
        assert disallowed.json() == {"detail": "Origin is not allowed."}
        assert calls == []
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()


def test_reconciliation_post_allowed_origin_and_exact_confirmation_reaches_service_once(monkeypatch):
    client = _client(monkeypatch)
    calls = []
    row = SimpleNamespace(
        id=PUBLICATION_ID,
        status="PUBLISHED",
        pinterest_pin_id="pin-test",
        published_at=None,
        error_code=None,
    )

    async def fake_reconcile(db, publication_id, *, actor):
        calls.append((publication_id, actor))
        return row

    monkeypatch.setattr(route, "reconcile_buffer", fake_reconcile)
    try:
        response = client.post(
            f"/api/publications/{PUBLICATION_ID}/reconcile-buffer",
            headers={"Origin": ALLOWED_ORIGIN},
            json=_payload(),
        )
        assert response.status_code == 200
        assert response.json()["status"] == "PUBLISHED"
        assert calls == [(PUBLICATION_ID, "operator")]
    finally:
        app.dependency_overrides.pop(get_db, None)
        get_settings.cache_clear()
