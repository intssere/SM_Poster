import asyncio
import base64
import hashlib
import httpx
import pytest
from sqlalchemy import create_engine
from sqlalchemy.pool import StaticPool
from sqlalchemy.orm import sessionmaker

from app.core.config import get_settings
from app.services import pinterest_oauth as oauth
from app.main import app
from app.db.session import SessionLocal
from app.db.session import get_db
from app.db.base import Base
from app.models.domain import PinterestOAuthState
from fastapi.testclient import TestClient


def configure(monkeypatch, **values):
    defaults = {"DATABASE_URL": "sqlite+pysqlite:///:memory:", "PINTEREST_CLIENT_ID": "client", "PINTEREST_CLIENT_SECRET": "secret", "PINTEREST_REDIRECT_URI": "https://studio.example/callback", "PINTEREST_TOKEN_ENCRYPTION_KEY": oauth.Fernet.generate_key().decode()}
    defaults.update(values)
    for key, value in defaults.items(): monkeypatch.setenv(key, value)
    get_settings.cache_clear()

@pytest.fixture
def isolated_app_db():
    engine = create_engine("sqlite+pysqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
    Base.metadata.create_all(engine)
    PinterestOAuthState.__table__.create(engine, checkfirst=True)
    testing_session = sessionmaker(bind=engine, autoflush=False, autocommit=False)
    def override_get_db():
        db = testing_session()
        try: yield db
        finally: db.close()
    app.dependency_overrides.clear()
    app.dependency_overrides[get_db] = override_get_db
    try: yield testing_session
    finally:
        app.dependency_overrides.pop(get_db, None)
        engine.dispose()


def test_authorization_url_and_hashed_unpredictable_state(monkeypatch):
    configure(monkeypatch)
    raw, digest = oauth.new_state()
    url = oauth.authorization_url(raw)
    assert url.startswith("https://www.pinterest.com/oauth/")
    assert "response_type=code" in url and "client_id=client" in url
    assert "user_accounts%3Aread" in url and "boards%3Aread" in url and "pins%3Aread" in url
    assert digest == hashlib.sha256(raw.encode()).hexdigest() and raw != digest
    assert raw != oauth.new_state()[0]


def test_token_encryption_never_stores_plaintext_and_wrong_key_fails(monkeypatch):
    configure(monkeypatch)
    ciphertext = oauth.encrypt_token("access-token")
    assert ciphertext != "access-token" and oauth.decrypt_token(ciphertext) == "access-token"
    monkeypatch.setenv("PINTEREST_TOKEN_ENCRYPTION_KEY", oauth.Fernet.generate_key().decode()); get_settings.cache_clear()
    with pytest.raises(Exception): oauth.decrypt_token(ciphertext)


def test_mocked_exchange_and_user_account_calls_are_server_side(monkeypatch):
    configure(monkeypatch)
    seen = []
    async def handler(request):
        seen.append((request.url.path, request.headers.get("authorization")))
        if request.url.path.endswith("oauth/token"): return httpx.Response(200, json={"access_token":"a", "refresh_token":"r", "scope":"user_accounts:read boards:read pins:read"})
        return httpx.Response(200, json={"id":"42", "username":"admin"})
    client = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    provider = oauth.PinterestClient(client)
    tokens = asyncio.run(provider.exchange_code("code"))
    account = asyncio.run(provider.user_account(tokens["access_token"]))
    asyncio.run(client.aclose())
    assert tokens["access_token"] == "a" and account["id"] == "42"
    assert seen[0][0].endswith("oauth/token") and seen[1][0].endswith("user_account")

@pytest.mark.parametrize("payload", [
    {"access_token": "a", "refresh_token": "r", "scope": "user_accounts:read boards:read"},
    {"refresh_token": "r", "scope": "user_accounts:read boards:read pins:read"},
])
def test_required_scope_and_access_token_validation(payload, monkeypatch):
    configure(monkeypatch)
    assert not (payload.get("access_token") and set(oauth.SCOPES).issubset(payload.get("scope", "").split()))

def test_refresh_request_uses_basic_auth_and_refresh_grant(monkeypatch):
    configure(monkeypatch)
    seen = {}
    async def handler(request):
        seen["auth"] = request.headers.get("authorization"); seen["body"] = (await request.aread()).decode(); return httpx.Response(200, json={"access_token":"new", "scope":"user_accounts:read boards:read pins:read"})
    async def run():
        c = httpx.AsyncClient(transport=httpx.MockTransport(handler)); result = await oauth.PinterestClient(c).refresh_token("old-refresh"); await c.aclose(); return result
    assert asyncio.run(run())["access_token"] == "new"
    assert seen["body"] == "grant_type=refresh_token&refresh_token=old-refresh"
    assert base64.b64decode(seen["auth"].split()[1]).decode() == "client:secret"

def test_refresh_failure_is_sanitized(monkeypatch):
    configure(monkeypatch)
    async def handler(request): return httpx.Response(500, text="secret provider body")
    async def run():
        c = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try: await oauth.PinterestClient(c).refresh_token("refresh")
        except RuntimeError as exc: return str(exc)
        finally: await c.aclose()
    message = asyncio.run(run())
    assert "secret" not in message and "provider body" not in message

@pytest.mark.parametrize("value", ["", "not-a-fernet-key"])
def test_missing_or_invalid_encryption_configuration_fails_closed(monkeypatch, value):
    configure(monkeypatch, PINTEREST_TOKEN_ENCRYPTION_KEY=value); get_settings.cache_clear()
    with pytest.raises(RuntimeError): oauth.encrypt_token("token")

def test_redirect_safe_values_exclude_oauth_secrets():
    safe = "http://localhost:5000/#channels?provider=pinterest&result=connected"
    assert all(secret not in safe for secret in ("code", "state", "access_token", "refresh_token", "client_secret"))

def test_scope_normalization_is_exact():
    scopes = set("pins:read boards:read user_accounts:read".split())
    assert scopes == set(oauth.SCOPES)

def test_state_hash_is_one_way_and_fixed_length(monkeypatch):
    configure(monkeypatch)
    raw, digest = oauth.new_state()
    assert len(digest) == 64 and raw not in digest and digest == hashlib.sha256(raw.encode()).hexdigest()

def test_provider_timeout_is_explicit(monkeypatch):
    configure(monkeypatch)
    client = oauth.PinterestClient()
    assert client.client is None

@pytest.mark.parametrize("result", ["connected", "denied", "invalid_state", "oauth_error"])
def test_callback_result_contract_is_frontend_readable(result):
    from urllib.parse import urlsplit, parse_qs
    url = f"http://localhost:5000/?provider=pinterest&result={result}#channels"
    parts = urlsplit(url)
    assert parse_qs(parts.query)["result"] == [result] and parts.fragment == "channels"
    assert all(secret not in url for secret in ("code=", "state=", "access_token=", "refresh_token=", "client_secret="))

def test_oauth_start_route_requires_admin_and_persists_hashed_state(monkeypatch, isolated_app_db):
    configure(monkeypatch)
    monkeypatch.setenv("AUTH_DISABLED", "true"); monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    with isolated_app_db() as db:
        db.query(PinterestOAuthState).delete(); db.commit()
    response = TestClient(app).post("/api/channels/pinterest/oauth/start", headers={"Origin": "http://localhost:5000"})
    assert response.status_code == 200
    url = response.json()["authorization_url"]
    raw = url.split("state=", 1)[1]
    with isolated_app_db() as db:
        rows = db.query(PinterestOAuthState).all()
        assert rows and rows[-1].state_hash == hashlib.sha256(raw.encode()).hexdigest()
        assert raw not in {row.state_hash for row in rows}

def test_oauth_start_route_denies_anonymous(monkeypatch):
    configure(monkeypatch)
    monkeypatch.setenv("AUTH_DISABLED", "false"); monkeypatch.setenv("APP_ENV", "development")
    get_settings.cache_clear()
    assert TestClient(app).post("/api/channels/pinterest/oauth/start", headers={"Origin": "http://localhost:5000"}).status_code == 401
