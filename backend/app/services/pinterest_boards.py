"""Read-only Pinterest board snapshot synchronization."""
from datetime import datetime, timezone
import re
import httpx
from sqlalchemy import select
from app.core.config import get_settings
from app.models.domain import PinterestConnection, PinterestBoard, PinterestBoardSection
from app.services.pinterest_oauth import decrypt_token, refresh_connection
from datetime import timedelta

PINTEREST_ACCESS_TOKEN_REFRESH_WINDOW = timedelta(minutes=5)
READ_ONLY_SYNC_EXPIRY_MARGIN = timedelta(minutes=5)
MAX_READ_ONLY_BOARDS = 1000
MAX_READ_ONLY_SECTIONS = 1000
MAX_READ_ONLY_DESCRIPTION_LENGTH = 4096
MAX_READ_ONLY_COVER_URL_LENGTH = 2048

MAX_BOARD_PAGES = 100
MAX_SECTION_PAGES = 100

class PinterestBoardClient:
    async def get(self, path, access_token, params=None):
        try:
            s = get_settings()
            async with httpx.AsyncClient(timeout=httpx.Timeout(10.0, connect=5.0)) as client:
                response = await client.get(f"{s.pinterest_api_base.rstrip('/')}{path}", params=params, headers={"Authorization": f"Bearer {access_token}"})
            if response.status_code >= 400: raise RuntimeError("Pinterest board request failed")
            payload = response.json()
            if not isinstance(payload, dict): raise RuntimeError("Pinterest board response invalid")
            return payload
        except RuntimeError:
            raise
        except Exception as exc:
            raise RuntimeError("Pinterest board request failed safely") from exc

def _int(value):
    return int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None

def _datetime(value):
    if value is None: return None
    if not isinstance(value, str): raise RuntimeError("Pinterest board response invalid")
    try: return datetime.fromisoformat(value.replace("Z", "+00:00"))
    except (TypeError, ValueError) as exc: raise RuntimeError("Pinterest board response invalid") from exc

def _page(payload):
    if not isinstance(payload, dict) or not isinstance(payload.get("items"), list):
        raise RuntimeError("Pinterest board response invalid")
    bookmark = payload.get("bookmark")
    if bookmark is not None and not isinstance(bookmark, str): raise RuntimeError("Pinterest pagination invalid")
    return payload["items"], bookmark

def _require_read_only_sync_token_fresh(connection: PinterestConnection) -> None:
    expiry = connection.access_token_expires_at
    # Missing, naive, or non-UTC expiry cannot prove refresh will be avoided.
    try:
        fresh = (
            isinstance(expiry, datetime)
            and expiry.tzinfo is not None
            and expiry.fold == 0
            and expiry.utcoffset() == timedelta(0)
            and expiry > datetime.now(timezone.utc)
            + PINTEREST_ACCESS_TOKEN_REFRESH_WINDOW
            + READ_ONLY_SYNC_EXPIRY_MARGIN
        )
    except (TypeError, ValueError, OverflowError):
        fresh = False
    if not fresh:
        raise RuntimeError("Pinterest read-only board sync requires a safely fresh token")

async def sync_boards(db, connection: PinterestConnection, client=None, *, provider_read_only=False):
    if connection.status != "CONNECTED" or not connection.access_token_ciphertext: raise RuntimeError("Pinterest account is not connected")
    if provider_read_only:
        _require_read_only_sync_token_fresh(connection)
    elif connection.access_token_expires_at is not None:
        expiry = connection.access_token_expires_at
        if expiry.tzinfo is None: expiry = expiry.replace(tzinfo=timezone.utc)
        if expiry <= datetime.now(timezone.utc) + PINTEREST_ACCESS_TOKEN_REFRESH_WINDOW:
            await refresh_connection(db, connection)
            db.refresh(connection)
    access = decrypt_token(connection.access_token_ciphertext); client = client or PinterestBoardClient(); now = datetime.now(timezone.utc)
    async def safe_get(path, params=None):
        if provider_read_only:
            _require_read_only_sync_token_fresh(connection)
        try: return await client.get(path, access, params)
        except RuntimeError as exc:
            raise RuntimeError("Pinterest board request failed safely") from exc
    boards, sections = [], [] ; bookmark = None; seen_bookmarks = set()
    for _ in range(MAX_BOARD_PAGES):
        if bookmark in seen_bookmarks: raise RuntimeError("Pinterest board pagination invalid")
        if bookmark: seen_bookmarks.add(bookmark)
        payload = await safe_get("/boards", {"bookmark": bookmark} if bookmark else None)
        items, bookmark = _page(payload)
        if provider_read_only and len(boards) + len(items) > MAX_READ_ONLY_BOARDS:
            raise RuntimeError("Pinterest read-only board sync item limit exceeded")
        boards.extend(items)
        if not bookmark: break
    else: raise RuntimeError("Pinterest board pagination limit exceeded")
    seen = set()
    for item in boards:
        if provider_read_only:
            _require_read_only_sync_token_fresh(connection)
        if not isinstance(item, dict) or not isinstance(item.get("id"), (str, int)) or isinstance(item.get("id"), bool) or not str(item.get("id")).strip(): raise RuntimeError("Pinterest board response invalid")
        external = str(item["id"])
        if provider_read_only and not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", external):
            raise RuntimeError("Pinterest board response invalid")
        seen.add(external)
        row = db.scalar(select(PinterestBoard).where(PinterestBoard.connection_id == connection.id, PinterestBoard.external_board_id == external))
        if not row: row = PinterestBoard(connection_id=connection.id, external_board_id=external); db.add(row)
        name = item.get("name")
        if not isinstance(name, str) or not name.strip() or len(name) > 255: raise RuntimeError("Pinterest board response invalid")
        owner = item.get("owner")
        media = item.get("media")
        if owner is not None and not isinstance(owner, dict): raise RuntimeError("Pinterest board response invalid")
        if media is not None and not isinstance(media, dict): raise RuntimeError("Pinterest board response invalid")
        description, privacy = item.get("description"), item.get("privacy")
        if description is not None and not isinstance(description, str): raise RuntimeError("Pinterest board response invalid")
        if privacy is not None and (not isinstance(privacy, str) or len(privacy) > 40): raise RuntimeError("Pinterest board response invalid")
        owner_name = owner.get("username") if owner is not None else None
        cover = media.get("image_cover_url") if media is not None else None
        if owner_name is not None and (not isinstance(owner_name, str) or len(owner_name) > 255): raise RuntimeError("Pinterest board response invalid")
        if cover is not None and not isinstance(cover, str): raise RuntimeError("Pinterest board response invalid")
        if provider_read_only and (
            isinstance(description, str) and len(description) > MAX_READ_ONLY_DESCRIPTION_LENGTH
            or isinstance(cover, str) and len(cover) > MAX_READ_ONLY_COVER_URL_LENGTH
        ):
            raise RuntimeError("Pinterest read-only board sync item limit exceeded")
        for field, value in (("name",name),("description",item.get("description")),("privacy",item.get("privacy")),("owner_username",owner_name),("image_cover_url",cover), ("board_pins_modified_at",_datetime(item.get("board_pins_modified_at"))), ("provider_created_at",_datetime(item.get("created_at")))):
            setattr(row, field, value)
        for field in ("pin_count","follower_count","collaborator_count"): setattr(row, field, _int(item.get(field)))
        ads_only = item.get("is_ads_only", False)
        if not isinstance(ads_only, bool): raise RuntimeError("Pinterest board response invalid")
        row.is_ads_only = ads_only; row.is_active = True; row.last_seen_at = now; row.last_synced_at = now
        if provider_read_only:
            _require_read_only_sync_token_fresh(connection)
        db.flush()
        bookmark_s = None; section_seen = set(); section_bookmarks = set()
        for _ in range(MAX_SECTION_PAGES):
            if bookmark_s in section_bookmarks: raise RuntimeError("Pinterest section pagination invalid")
            if bookmark_s: section_bookmarks.add(bookmark_s)
            section_payload = await safe_get(f"/boards/{external}/sections", {"bookmark": bookmark_s} if bookmark_s else None)
            section_items, bookmark_s = _page(section_payload)
            if provider_read_only and len(sections) + len(section_items) > MAX_READ_ONLY_SECTIONS:
                raise RuntimeError("Pinterest read-only board sync item limit exceeded")
            for x in section_items:
                if not isinstance(x, dict) or not isinstance(x.get("id"), (str, int)) or isinstance(x.get("id"), bool) or not str(x.get("id")).strip(): raise RuntimeError("Pinterest section response invalid")
                if provider_read_only and not re.fullmatch(r"[A-Za-z0-9_-]{1,255}", str(x["id"])):
                    raise RuntimeError("Pinterest section response invalid")
                sections.append((row, x)); section_seen.add(str(x.get("id")))
            if not bookmark_s: break
        else: raise RuntimeError("Pinterest section pagination limit exceeded")
        if provider_read_only:
            _require_read_only_sync_token_fresh(connection)
        for existing in db.scalars(select(PinterestBoardSection).where(PinterestBoardSection.board_id == row.id)):
            if existing.external_section_id not in section_seen: existing.is_active = False
    if provider_read_only:
        _require_read_only_sync_token_fresh(connection)
    for board in db.scalars(select(PinterestBoard).where(PinterestBoard.connection_id == connection.id)):
        if board.external_board_id not in seen:
            board.is_active = False
            if provider_read_only:
                _require_read_only_sync_token_fresh(connection)
            for section in db.scalars(select(PinterestBoardSection).where(PinterestBoardSection.board_id == board.id)):
                section.is_active = False
    for board, item in sections:
        if not isinstance(item, dict) or not item.get("id"): raise RuntimeError("Pinterest section response invalid")
        if provider_read_only:
            _require_read_only_sync_token_fresh(connection)
        section = db.scalar(select(PinterestBoardSection).where(PinterestBoardSection.board_id == board.id, PinterestBoardSection.external_section_id == str(item["id"])))
        if not section: section = PinterestBoardSection(board_id=board.id, external_section_id=str(item["id"])); db.add(section)
        section_name = item.get("name")
        if not isinstance(section_name, str) or not section_name.strip() or len(section_name) > 255: raise RuntimeError("Pinterest section response invalid")
        section.name = section_name; section.is_active = True; section.last_seen_at = now; section.last_synced_at = now
    if provider_read_only:
        _require_read_only_sync_token_fresh(connection)
    connection.boards_last_synced_at = now
    if provider_read_only:
        _require_read_only_sync_token_fresh(connection)
    db.commit(); return len(boards)

def eligible_boards(db, connection_id):
    return list(db.scalars(select(PinterestBoard).where(PinterestBoard.connection_id == connection_id, PinterestBoard.is_active.is_(True), PinterestBoard.is_eligible.is_(True))))

def validate_publication_board(db, connection_id, board_id):
    connection = db.get(PinterestConnection, connection_id)
    if not connection or connection.status != "CONNECTED": raise ValueError("Pinterest account is not connected")
    row = db.scalar(select(PinterestBoard).where(PinterestBoard.id == board_id, PinterestBoard.connection_id == connection_id, PinterestBoard.is_active.is_(True), PinterestBoard.is_eligible.is_(True)))
    if not row: raise ValueError("Pinterest board is not eligible")
    return row
