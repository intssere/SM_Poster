from __future__ import annotations
import base64, hashlib, secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import httpx
from cryptography.fernet import Fernet
from app.core.config import get_settings
from app.models.domain import PinterestConnection

SCOPES = ("user_accounts:read", "boards:read", "pins:read")

def _fernet() -> Fernet:
    key = get_settings().pinterest_token_encryption_key
    if not key: raise RuntimeError("Pinterest token encryption is not configured")
    try: return Fernet(key.encode())
    except Exception as exc: raise RuntimeError("Pinterest token encryption is invalid") from exc

def encrypt_token(value: str) -> str: return _fernet().encrypt(value.encode()).decode()
def decrypt_token(value: str) -> str: return _fernet().decrypt(value.encode()).decode()
def new_state() -> tuple[str, str]:
    raw = secrets.token_urlsafe(32)
    return raw, hashlib.sha256(raw.encode()).hexdigest()

def authorization_url(state: str) -> str:
    s = get_settings()
    if not s.pinterest_client_id or not s.pinterest_redirect_uri: raise RuntimeError("Pinterest OAuth is not configured")
    return "https://www.pinterest.com/oauth/?" + urlencode({"client_id": s.pinterest_client_id, "redirect_uri": s.pinterest_redirect_uri, "response_type": "code", "scope": " ".join(SCOPES), "state": state})

class PinterestClient:
    def __init__(self, client: httpx.AsyncClient | None = None): self.client = client
    async def exchange_code(self, code: str) -> dict:
        s = get_settings()
        if not s.pinterest_client_id or not s.pinterest_client_secret or not s.pinterest_redirect_uri: raise RuntimeError("Pinterest OAuth is not configured")
        headers = {"Content-Type": "application/x-www-form-urlencoded"}
        data = {"grant_type":"authorization_code", "code":code, "redirect_uri":s.pinterest_redirect_uri}
        owned = self.client is None; c = self.client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        try: r = await c.post(f"{s.pinterest_api_base.rstrip('/')}/oauth/token", data=data, headers=headers, auth=httpx.BasicAuth(s.pinterest_client_id, s.pinterest_client_secret))
        finally:
            if owned: await c.aclose()
        if r.status_code >= 400: raise RuntimeError("Pinterest token exchange failed")
        return r.json()

    async def refresh_token(self, refresh_token: str) -> dict:
        s = get_settings()
        if not s.pinterest_client_id or not s.pinterest_client_secret: raise RuntimeError("Pinterest OAuth is not configured")
        data = {"grant_type": "refresh_token", "refresh_token": refresh_token}
        owned = self.client is None; c = self.client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        try: r = await c.post(f"{s.pinterest_api_base.rstrip('/')}/oauth/token", data=data, auth=httpx.BasicAuth(s.pinterest_client_id, s.pinterest_client_secret))
        finally:
            if owned: await c.aclose()
        if r.status_code >= 400: raise RuntimeError("Pinterest token refresh failed")
        payload = r.json()
        if not payload.get("access_token"): raise RuntimeError("Pinterest token refresh response was invalid")
        return payload

    async def user_account(self, access_token: str) -> dict:
        s = get_settings()
        owned = self.client is None; c = self.client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        try: r = await c.get(f"{s.pinterest_api_base.rstrip('/')}/user_account", headers={"Authorization": f"Bearer {access_token}"})
        finally:
            if owned: await c.aclose()
        if r.status_code >= 400: raise RuntimeError("Pinterest account verification failed")
        return r.json()

async def refresh_connection(db, connection: PinterestConnection, client: PinterestClient | None = None) -> PinterestConnection:
    old_access, old_refresh = connection.access_token_ciphertext, connection.refresh_token_ciphertext
    try:
        payload = await (client or PinterestClient()).refresh_token(decrypt_token(old_refresh))
        scopes = payload.get("scope", connection.granted_scopes)
        scopes = scopes.split() if isinstance(scopes, str) else list(scopes or [])
        if not payload.get("access_token") or not set(SCOPES).issubset(scopes): raise RuntimeError("Pinterest token refresh did not grant required access")
        connection.access_token_ciphertext = encrypt_token(payload["access_token"])
        if payload.get("refresh_token"): connection.refresh_token_ciphertext = encrypt_token(payload["refresh_token"])
        now = datetime.now(timezone.utc)
        connection.access_token_expires_at = now + timedelta(seconds=int(payload.get("expires_in", 0))) if payload.get("expires_in") else connection.access_token_expires_at
        if payload.get("refresh_token_expires_in"): connection.refresh_token_expires_at = now + timedelta(seconds=int(payload["refresh_token_expires_in"]))
        connection.granted_scopes, connection.refreshed_at, connection.last_error_code = scopes, now, None
        db.commit(); return connection
    except Exception as exc:
        connection.access_token_ciphertext, connection.refresh_token_ciphertext = old_access, old_refresh
        connection.last_error_code = "TOKEN_REFRESH_FAILED"; db.commit(); raise RuntimeError("Pinterest token refresh failed safely") from exc
