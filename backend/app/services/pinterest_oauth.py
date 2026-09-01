from __future__ import annotations
import base64, hashlib, secrets
from datetime import datetime, timedelta, timezone
from urllib.parse import urlencode
import httpx
from cryptography.fernet import Fernet
from app.core.config import get_settings

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
        data = {"grant_type":"authorization_code", "code":code, "redirect_uri":s.pinterest_redirect_uri, "client_id":s.pinterest_client_id, "client_secret":s.pinterest_client_secret}
        owned = self.client is None; c = self.client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        try: r = await c.post(f"{s.pinterest_api_base.rstrip('/')}/oauth/token", data=data, headers=headers)
        finally:
            if owned: await c.aclose()
        if r.status_code >= 400: raise RuntimeError("Pinterest token exchange failed")
        return r.json()
    async def user_account(self, access_token: str) -> dict:
        s = get_settings()
        owned = self.client is None; c = self.client or httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0))
        try: r = await c.get(f"{s.pinterest_api_base.rstrip('/')}/user_account", headers={"Authorization": f"Bearer {access_token}"})
        finally:
            if owned: await c.aclose()
        if r.status_code >= 400: raise RuntimeError("Pinterest account verification failed")
        return r.json()
