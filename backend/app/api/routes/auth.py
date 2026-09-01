from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request, Response, status
from pydantic import BaseModel, Field

from app.core.auth import SESSION_COOKIE, auth_configured, auth_is_bypassed, current_user, make_session, password_matches
from app.core.config import get_settings

router = APIRouter(prefix="/auth", tags=["auth"])


class LoginRequest(BaseModel):
    username: str = Field(min_length=1, max_length=128)
    password: str = Field(min_length=1, max_length=512)


@router.get("/status")
def auth_status(request: Request):
    user = current_user(request)
    return {"authenticated": user is not None, "username": user}


@router.post("/login")
def login(body: LoginRequest, response: Response):
    settings = get_settings()
    if not auth_is_bypassed() and not auth_configured():
        if settings.is_exposed:
            raise HTTPException(status_code=503, detail="Authentication is not configured.")
        raise HTTPException(status_code=503, detail="Authentication is not configured for this environment.")
    if auth_is_bypassed() or (body.username == settings.admin_username and password_matches(body.password)):
        response.set_cookie(SESSION_COOKIE, make_session(body.username), httponly=True, secure=settings.is_exposed, samesite="strict", max_age=settings.auth_session_ttl_seconds, path="/")
        return {"authenticated": True, "username": body.username}
    raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Invalid credentials")


@router.post("/logout")
def logout(request: Request, response: Response):
    if not current_user(request):
        raise HTTPException(status_code=401, detail="Authentication required")
    response.delete_cookie(SESSION_COOKIE, path="/")
    return {"authenticated": False}
