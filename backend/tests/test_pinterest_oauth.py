import asyncio
import base64
import hashlib
import httpx
import pytest

from app.core.config import get_settings
from app.services import pinterest_oauth as oauth


def configure(monkeypatch, **values):
    defaults = {"PINTEREST_CLIENT_ID": "client", "PINTEREST_CLIENT_SECRET": "secret", "PINTEREST_REDIRECT_URI": "https://studio.example/callback", "PINTEREST_TOKEN_ENCRYPTION_KEY": oauth.Fernet.generate_key().decode()}
    defaults.update(values)
    for key, value in defaults.items(): monkeypatch.setenv(key, value)
    get_settings.cache_clear()


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
