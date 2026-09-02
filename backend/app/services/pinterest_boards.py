"""Read-only Pinterest board snapshot synchronization."""
from datetime import datetime, timezone
import httpx
from sqlalchemy import select
from app.core.config import get_settings
from app.models.domain import PinterestConnection, PinterestBoard, PinterestBoardSection
from app.services.pinterest_oauth import decrypt_token

class PinterestBoardClient:
    async def get(self, path, access_token, params=None):
        s = get_settings()
        async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
            response = await client.get(f"{s.pinterest_api_base.rstrip('/')}{path}", params=params, headers={"Authorization": f"Bearer {access_token}"})
        if response.status_code >= 400: raise RuntimeError("Pinterest board request failed")
        payload = response.json()
        if not isinstance(payload, dict): raise RuntimeError("Pinterest board response invalid")
        return payload

def _int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

async def sync_boards(db, connection: PinterestConnection, client=None):
    if connection.status != "CONNECTED" or not connection.access_token_ciphertext: raise RuntimeError("Pinterest account is not connected")
    access = decrypt_token(connection.access_token_ciphertext); client = client or PinterestBoardClient(); now = datetime.now(timezone.utc)
    boards, sections = [], [] ; bookmark = None
    while True:
        payload = await client.get("/v5/boards", access, {"bookmark": bookmark} if bookmark else None)
        boards.extend(payload.get("items", [])); bookmark = payload.get("bookmark")
        if not bookmark: break
    seen = set()
    for item in boards:
        if not isinstance(item, dict) or not item.get("id"): raise RuntimeError("Pinterest board response invalid")
        external = str(item["id"]); seen.add(external)
        row = db.scalar(select(PinterestBoard).where(PinterestBoard.connection_id == connection.id, PinterestBoard.external_board_id == external))
        if not row: row = PinterestBoard(connection_id=connection.id, external_board_id=external); db.add(row)
        for field, key in (("name","name"),("description","description"),("privacy","privacy"),("owner_username","owner_username"),("image_cover_url","image_cover_url")):
            setattr(row, field, item.get(key))
        for field in ("pin_count","follower_count","collaborator_count"): setattr(row, field, _int(item.get(field)))
        row.is_ads_only = bool(item.get("is_ads_only", False)); row.is_active = True; row.last_seen_at = now; row.last_synced_at = now
        db.flush()
        bookmark_s = None
        while True:
            section_payload = await client.get(f"/v5/boards/{external}/sections", access, {"bookmark": bookmark_s} if bookmark_s else None)
            sections.extend((row, x) for x in section_payload.get("items", [])); bookmark_s = section_payload.get("bookmark")
            if not bookmark_s: break
    for board in db.scalars(select(PinterestBoard).where(PinterestBoard.connection_id == connection.id)):
        if board.external_board_id not in seen: board.is_active = False
    for board, item in sections:
        if not isinstance(item, dict) or not item.get("id"): raise RuntimeError("Pinterest section response invalid")
        section = db.scalar(select(PinterestBoardSection).where(PinterestBoardSection.board_id == board.id, PinterestBoardSection.external_section_id == str(item["id"])))
        if not section: section = PinterestBoardSection(board_id=board.id, external_section_id=str(item["id"])); db.add(section)
        section.name = item.get("name") or ""; section.is_active = True; section.last_seen_at = now; section.last_synced_at = now
    db.commit(); return len(boards)

def eligible_boards(db, connection_id):
    return list(db.scalars(select(PinterestBoard).where(PinterestBoard.connection_id == connection_id, PinterestBoard.is_active.is_(True), PinterestBoard.is_eligible.is_(True))))
