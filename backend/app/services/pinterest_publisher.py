"""Provider dispatch boundary; never invoked automatically."""
from app.core.config import get_settings
from datetime import datetime, timezone
from app.models.domain import PublicationStatus, PinterestConnection, PinterestBoard, PublicationAttempt
from app.integrations.pinterest.gateway import PinterestPinPayload
from app.integrations.pinterest.gateway import PinterestDefinitiveRejection, PinterestAmbiguousFailure

class PublicationReconciliationError(RuntimeError):
    pass
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
    if publication.status not in {PublicationStatus.SCHEDULED, PublicationStatus.PUBLISHING}:
        return False, "INVALID_PUBLICATION_STATE"
    if not all((publication.publication_fingerprint, publication.pinterest_connection_id, publication.pinterest_board_record_id, publication.pinterest_board_id_snapshot, publication.title_snapshot, publication.description_snapshot, publication.destination_url, publication.media_url_snapshot)):
        return False, "INCOMPLETE_SNAPSHOT"
    if not settings.publishing_enabled:
        return False, "PUBLISHING_DISABLED"
    connection = db.get(PinterestConnection, publication.pinterest_connection_id)
    board = db.get(PinterestBoard, publication.pinterest_board_record_id)
    if not connection or connection.status != "CONNECTED" or "pins:write" not in (connection.granted_scopes or []):
        return False, "PUBLISHING_SCOPE_REQUIRED"
    if not board or board.connection_id != connection.id or not board.is_active or not board.is_eligible:
        return False, "INVALID_DESTINATION"
    if board.external_board_id != publication.pinterest_board_id_snapshot:
        return False, "DESTINATION_MISMATCH"
    from app.models.domain import PinApproval, PinCreative
    approval = db.get(PinApproval, publication.approval_id) if publication.approval_id else None
    creative = db.get(PinCreative, publication.creative_id)
    if not approval or approval.decision != "APPROVED" or approval.draft_id != publication.draft_id or approval.revision_id != publication.revision_id or approval.creative_id != publication.creative_id:
        return False, "INVALID_APPROVAL"
    if not creative or creative.draft_id != publication.draft_id or creative.source_image_id != publication.source_image_id:
        return False, "INVALID_CREATIVE"
    media = publication.media_url_snapshot
    if not media_publishable(media):
        return False, "MEDIA_NOT_PUBLISHABLE"
    return True, None

def preflight_publish_readiness(db, publication):
    from datetime import datetime, timezone
    if publication.status != PublicationStatus.SCHEDULED or not publication.scheduled_for or publication.scheduled_for > datetime.now(timezone.utc):
        return False, "NOT_DUE"
    return publishing_ready(db, publication)

def execution_publish_readiness(db, publication, attempt):
    from app.services.publication_scheduler import request_fingerprint_for
    if not attempt or attempt.status != "STARTED" or attempt.publication_id != publication.id or attempt.request_fingerprint != request_fingerprint_for(publication):
        return False, "ATTEMPT_MISMATCH"
    return publishing_ready(db, publication)

def finalize_post_claim_unknown(db, publication, attempt, code="POST_CLAIM_REVALIDATION_FAILED"):
    from datetime import datetime, timezone
    attempt.status = "UNKNOWN"; attempt.error_code = code; attempt.completed_at = datetime.now(timezone.utc)
    publication.status = PublicationStatus.PUBLISH_UNKNOWN; publication.error_code = code
    try:
        db.commit()
    except Exception:
        db.rollback()
        raise PublicationReconciliationError("Publication reconciliation could not be persisted") from None
    return publication

async def publish_once(db, publication, gateway, attempt=None):
    if publication.status != PublicationStatus.PUBLISHING or attempt is None or attempt.publication_id != publication.id or attempt.status != "STARTED":
        if attempt is not None: finalize_post_claim_unknown(db, publication, attempt, "ATTEMPT_IDENTITY_MISMATCH")
        raise RuntimeError("ATTEMPT_IDENTITY_MISMATCH")
    ready, reason = publishing_ready(db, publication)
    if not ready:
        finalize_post_claim_unknown(db, publication, attempt, reason)
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
        attempt.status = "SUCCEEDED"; attempt.provider_pin_id = pin_id; attempt.safe_response_metadata = {"validated_pin_id": pin_id}; attempt.completed_at = datetime.now(timezone.utc)
        publication.status = PublicationStatus.PUBLISHED; publication.pinterest_pin_id = pin_id; publication.published_at = datetime.now(timezone.utc)
        db.commit()
        return result
    except Exception as exc:
        db.rollback(); attempt = db.get(PublicationAttempt, attempt.id)
        attempt.status = "FAILED" if isinstance(exc, PinterestDefinitiveRejection) else "UNKNOWN"
        attempt.error_code = "PUBLISH_UNKNOWN" if attempt.status == "UNKNOWN" else "PROVIDER_REJECTED"
        attempt.completed_at = datetime.now(timezone.utc)
        publication.status = PublicationStatus.PUBLISH_UNKNOWN if attempt.status == "UNKNOWN" else PublicationStatus.PUBLISH_FAILED
        publication.error_code = attempt.error_code
        db.commit()
        raise RuntimeError(attempt.error_code) from None
