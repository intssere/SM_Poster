"""Provider dispatch boundary; never invoked automatically."""
from app.core.config import get_settings
from app.models.domain import PublicationStatus, PinterestConnection, PinterestBoard
from app.integrations.pinterest.gateway import PinterestPinPayload

def publishing_ready(db, publication):
    settings = get_settings()
    if not settings.publishing_enabled:
        return False, "PUBLISHING_DISABLED"
    connection = db.get(PinterestConnection, publication.pinterest_connection_id)
    board = db.get(PinterestBoard, publication.pinterest_board_record_id)
    if not connection or connection.status != "CONNECTED" or "pins:write" not in (connection.granted_scopes or []):
        return False, "PUBLISHING_SCOPE_REQUIRED"
    if not board or board.connection_id != connection.id or not board.is_active or not board.is_eligible:
        return False, "INVALID_DESTINATION"
    media = publication.media_url_snapshot
    if not media or not media.startswith("https://"):
        return False, "MEDIA_NOT_PUBLISHABLE"
    return True, None

async def publish_once(db, publication, gateway):
    ready, reason = publishing_ready(db, publication)
    if not ready:
        raise RuntimeError(reason)
    payload = PinterestPinPayload(
        board_id=publication.pinterest_board_id_snapshot or publication.pinterest_board_id or "",
        title=publication.title_snapshot or "",
        description=publication.description_snapshot or "",
        link=publication.destination_url or "",
        image_url=publication.media_url_snapshot,
        alt_text=publication.alt_text_snapshot,
    )
    return await gateway.create_pin(payload)
