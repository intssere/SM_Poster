from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.models.domain import PinterestOAuthState, PinterestConnection
from app.services.pinterest_oauth import authorization_url, new_state, PinterestClient, encrypt_token

from app.services.social_channels import channel_capability_payload


router = APIRouter(prefix="/channels", tags=["social-channels"])


@router.get("/capabilities")
def channel_capabilities():
    """Return read-only channel capability metadata; no provider is contacted."""
    return channel_capability_payload(publishing_enabled=False)

@router.get("/pinterest/status")
def pinterest_status(db: Session = Depends(get_db)):
    row = db.scalar(select(PinterestConnection).where(PinterestConnection.status == "CONNECTED"))
    return {"status": "CONNECTED" if row else "NOT_CONNECTED", "connected": bool(row), "account": ({"id": row.external_user_id, "username": row.username, "status": row.status, "granted_scopes": row.granted_scopes, "access_token_expires_at": row.access_token_expires_at, "refresh_token_expires_at": row.refresh_token_expires_at} if row else None)}

def pinterest_connect(db: Session = Depends(get_db)):
    raw, digest = new_state()
    state = PinterestOAuthState(state_hash=digest, initiated_by="admin", expires_at=datetime.now(timezone.utc) + timedelta(minutes=10))
    db.add(state); db.commit()
    return {"authorization_url": authorization_url(raw)}

@router.post("/pinterest/oauth/start")
def pinterest_oauth_start(db: Session = Depends(get_db)):
    return pinterest_connect(db)

@router.get("/pinterest/callback")
async def pinterest_callback(code: str | None = Query(default=None), state: str | None = Query(default=None), db: Session = Depends(get_db)):
    if not code or not state: raise HTTPException(400, "OAuth callback is missing required parameters")
    digest = __import__("hashlib").sha256(state.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    claimed = db.execute(update(PinterestOAuthState).where(PinterestOAuthState.state_hash == digest, PinterestOAuthState.consumed_at.is_(None), PinterestOAuthState.expires_at >= now).values(consumed_at=now))
    if claimed.rowcount != 1: db.rollback(); raise HTTPException(400, "Invalid or expired OAuth state")
    db.commit()
    tokens = await PinterestClient().exchange_code(code)
    account = await PinterestClient().user_account(tokens["access_token"])
    connection = PinterestConnection(external_user_id=str(account.get("id") or account.get("username")), username=account.get("username"), granted_scopes=tokens.get("scope", "").split(), access_token_ciphertext=encrypt_token(tokens["access_token"]), refresh_token_ciphertext=encrypt_token(tokens.get("refresh_token", "")), token_type=tokens.get("token_type"))
    db.query(PinterestConnection).filter(PinterestConnection.status == "CONNECTED").update({"status":"DISCONNECTED", "disconnected_at":now})
    db.add(connection); db.commit()
    return {"connected": True, "account": {"id": connection.external_user_id, "username": connection.username}}

@router.post("/pinterest/disconnect")
def pinterest_disconnect(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    rows = db.query(PinterestConnection).filter(PinterestConnection.status == "CONNECTED").all()
    for row in rows: row.status = "DISCONNECTED"; row.disconnected_at = now; row.access_token_ciphertext = ""; row.refresh_token_ciphertext = ""
    db.commit(); return {"connected": False}
