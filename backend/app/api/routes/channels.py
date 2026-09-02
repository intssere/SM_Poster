from __future__ import annotations

from datetime import datetime, timedelta, timezone
from fastapi import APIRouter, Depends, HTTPException, Query
from fastapi.responses import RedirectResponse
from pydantic import BaseModel, ConfigDict, Field
from urllib.parse import urlsplit, urlunsplit, parse_qsl, urlencode
from sqlalchemy import select, update
from sqlalchemy.orm import Session
from app.db.session import get_db
from app.core.config import get_settings
from app.models.domain import PinterestOAuthState, PinterestConnection, PinterestBoard, PinterestBoardSection
from app.services.pinterest_boards import sync_boards
from app.services.pinterest_oauth import authorization_url, new_state, PinterestClient, encrypt_token, SCOPES

from app.services.social_channels import channel_capability_payload


router = APIRouter(prefix="/channels", tags=["social-channels"])

class BoardConfigPatch(BaseModel):
    model_config = ConfigDict(extra="forbid")
    is_eligible: bool | None = None
    routing_label: str | None = Field(default=None, max_length=120)

def _board_payload(db, r):
    return {"id": r.id, "external_board_id": r.external_board_id, "name": r.name, "description": r.description, "privacy": r.privacy, "owner_username": r.owner_username, "pin_count": r.pin_count, "follower_count": r.follower_count, "collaborator_count": r.collaborator_count, "is_ads_only": r.is_ads_only, "image_cover_url": r.image_cover_url, "board_pins_modified_at": r.board_pins_modified_at, "provider_created_at": r.provider_created_at, "is_active": r.is_active, "is_eligible": r.is_eligible, "routing_label": r.routing_label, "last_seen_at": r.last_seen_at, "last_synced_at": r.last_synced_at, "sections": [{"id": s.id, "external_section_id": s.external_section_id, "name": s.name, "is_active": s.is_active, "last_seen_at": s.last_seen_at, "last_synced_at": s.last_synced_at} for s in db.scalars(select(PinterestBoardSection).where(PinterestBoardSection.board_id == r.id)).all()]}

@router.post("/pinterest/boards/sync")
async def pinterest_boards_sync(db: Session = Depends(get_db)):
    connection = db.scalar(select(PinterestConnection).where(PinterestConnection.status == "CONNECTED"))
    if not connection: raise HTTPException(status_code=409, detail="Pinterest account is not connected")
    try: count = await sync_boards(db, connection)
    except RuntimeError as exc: db.rollback(); raise HTTPException(status_code=502, detail=str(exc))
    rows = db.scalars(select(PinterestBoard).where(PinterestBoard.connection_id == connection.id)).all()
    return {"connection_status": "CONNECTED", "boards": [_board_payload(db, r) for r in rows], "sync": {"boards_seen": count}}

@router.get("/pinterest/boards")
def pinterest_boards(db: Session = Depends(get_db)):
    connection = db.scalar(select(PinterestConnection).where(PinterestConnection.status == "CONNECTED"))
    if not connection: return {"status": "NOT_CONNECTED", "boards": []}
    rows = db.scalars(select(PinterestBoard).where(PinterestBoard.connection_id == connection.id)).all()
    return {"status": "CONNECTED", "connection_status": "CONNECTED", "boards": [_board_payload(db, r) for r in rows]}

@router.patch("/pinterest/boards/{board_id}")
def pinterest_board_config(board_id: str, payload: BoardConfigPatch, db: Session = Depends(get_db)):
    row = db.get(PinterestBoard, board_id)
    if not row: raise HTTPException(status_code=404, detail="Board not found")
    values = payload.model_dump(exclude_unset=True)
    if "is_eligible" in values and values["is_eligible"] is not None: row.is_eligible = values["is_eligible"]
    if "routing_label" in values: row.routing_label = values["routing_label"]
    db.commit(); return {"id": row.id, "is_eligible": row.is_eligible, "routing_label": row.routing_label}

def _result_redirect(result: str) -> str:
    target = get_settings().frontend_return_url
    parts = urlsplit(target)
    query = dict(parse_qsl(parts.query)); query.update({"provider": "pinterest", "result": result})
    return urlunsplit((parts.scheme, parts.netloc, parts.path, urlencode(query), parts.fragment or "channels"))


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
async def pinterest_callback(code: str | None = Query(default=None), state: str | None = Query(default=None), error: str | None = Query(default=None), db: Session = Depends(get_db)):
    if not state: return RedirectResponse(_result_redirect("oauth_error"))
    digest = __import__("hashlib").sha256(state.encode()).hexdigest()
    now = datetime.now(timezone.utc)
    claimed = db.execute(update(PinterestOAuthState).where(PinterestOAuthState.state_hash == digest, PinterestOAuthState.consumed_at.is_(None), PinterestOAuthState.expires_at >= now).values(consumed_at=now))
    if claimed.rowcount != 1: db.rollback(); return RedirectResponse(_result_redirect("invalid_state"))
    db.commit()
    if error:
        return RedirectResponse(_result_redirect("denied"))
    if not code:
        return RedirectResponse(_result_redirect("oauth_error"))
    try:
        tokens = await PinterestClient().exchange_code(code)
        if not isinstance(tokens, dict): raise RuntimeError("invalid token payload")
        scopes = tokens.get("scope", "")
        scopes = scopes.split() if isinstance(scopes, str) else list(scopes or [])
        if not tokens.get("access_token") or not tokens.get("refresh_token") or not set(SCOPES).issubset(scopes): raise RuntimeError("Pinterest authorization did not grant required access")
        account = await PinterestClient().user_account(tokens["access_token"])
        if not isinstance(account, dict): raise RuntimeError("invalid account payload")
        external_id = account.get("id")
        if isinstance(external_id, bool) or not isinstance(external_id, (str, int)) or not str(external_id).strip(): raise RuntimeError("invalid account identity")
        def expiry(name):
            value = tokens.get(name)
            return now + timedelta(seconds=int(value)) if value is not None else None
        access_cipher = encrypt_token(tokens["access_token"]); refresh_cipher = encrypt_token(tokens["refresh_token"])
        connection = PinterestConnection(external_user_id=str(external_id).strip(), username=account.get("username"), account_type=account.get("account_type"), profile_image_url=account.get("profile_image_url"), granted_scopes=scopes, access_token_ciphertext=access_cipher, refresh_token_ciphertext=refresh_cipher, access_token_expires_at=expiry("expires_in"), refresh_token_expires_at=expiry("refresh_token_expires_in"), last_verified_at=now, token_type=tokens.get("token_type"))
        db.query(PinterestConnection).filter(PinterestConnection.status == "CONNECTED").update({"status":"DISCONNECTED", "disconnected_at":now})
        db.add(connection); db.commit()
    except Exception:
        db.rollback()
        return RedirectResponse(_result_redirect("oauth_error"))
    return RedirectResponse(_result_redirect("connected"))

@router.post("/pinterest/disconnect")
def pinterest_disconnect(db: Session = Depends(get_db)):
    now = datetime.now(timezone.utc)
    rows = db.query(PinterestConnection).filter(PinterestConnection.status == "CONNECTED").all()
    for row in rows: row.status = "DISCONNECTED"; row.disconnected_at = now; row.access_token_ciphertext = ""; row.refresh_token_ciphertext = ""
    db.commit(); return {"connected": False}
