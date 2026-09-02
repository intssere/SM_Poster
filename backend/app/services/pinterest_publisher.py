"""Provider dispatch boundary; never invoked automatically."""
from app.core.config import get_settings
from datetime import datetime, timezone
from app.models.domain import PublicationStatus, PinterestConnection, PinterestBoard, PublicationAttempt
from app.integrations.pinterest.gateway import PinterestPinPayload
from urllib.parse import urlsplit
import ipaddress

def media_publishable(value):
    try:
        p = urlsplit(value or "")
        if p.scheme != "https" or not p.hostname or p.username or p.password:
            return False
        host = p.hostname.lower().rstrip(".")
        if host == "localhost" or host.endswith(".localhost") or host.endswith(".local"):
            return False
        try:
            ip = ipaddress.ip_address(host)
            if ip.is_private or ip.is_loopback or ip.is_link_local or ip.is_reserved or ip.is_unspecified or ip.is_multicast:
                return False
        except ValueError:
            pass
        return True
    except ValueError:
        return False

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
    if not media_publishable(media):
        return False, "MEDIA_NOT_PUBLISHABLE"
    return True, None

async def publish_once(db, publication, gateway, attempt=None):
    if publication.status != PublicationStatus.PUBLISHING or attempt is None or attempt.publication_id != publication.id or attempt.status != "STARTED":
        raise RuntimeError("INVALID_PUBLISH_ATTEMPT")
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
    try:
        result = await gateway.create_pin(payload)
        pin_id = result.get("id") if isinstance(result, dict) else None
        if not isinstance(pin_id, str) or not pin_id.strip():
            raise RuntimeError("AMBIGUOUS_PROVIDER_RESULT")
        attempt.status = "SUCCEEDED"; attempt.provider_pin_id = pin_id; attempt.completed_at = datetime.now(timezone.utc)
        publication.status = PublicationStatus.PUBLISHED; publication.pinterest_pin_id = pin_id; publication.published_at = datetime.now(timezone.utc)
        db.commit()
        return result
    except Exception as exc:
        db.rollback(); attempt = db.get(PublicationAttempt, attempt.id)
        attempt.status = "UNKNOWN" if "timeout" in str(exc).lower() or "reset" in str(exc).lower() or "ambiguous" in str(exc).lower() else "FAILED"
        attempt.error_code = "PUBLISH_UNKNOWN" if attempt.status == "UNKNOWN" else "PROVIDER_REJECTED"
        attempt.completed_at = datetime.now(timezone.utc)
        publication.status = PublicationStatus.PUBLISH_UNKNOWN if attempt.status == "UNKNOWN" else PublicationStatus.PUBLISH_FAILED
        publication.error_code = attempt.error_code
        db.commit()
        raise RuntimeError(attempt.error_code) from None
