from __future__ import annotations

from sqlalchemy import select
from app.core.config import get_settings
from app.models.domain import PinPublication, PublicationAttempt, PublicationReconciliationEvent, PinterestBoard
from app.services.pinterest_publisher import sanitize_metadata
from app.services.publication_dispatch_authorization import readiness_result, CONFIRMATION_TEXT_VERSION

CONFIRMATION_PROMPT = "I confirm that I reviewed this exact approved publication, destination, creative, and validation result for future manual Pinterest dispatch."

def build_preview(db, publication: PinPublication) -> dict:
    readiness = readiness_result(db, publication)
    board = db.get(PinterestBoard, publication.pinterest_board_record_id) if publication.pinterest_board_record_id else None
    attempts = db.scalars(select(PublicationAttempt).where(PublicationAttempt.publication_id == publication.id).order_by(PublicationAttempt.attempt_number)).all()
    events = db.scalars(select(PublicationReconciliationEvent).where(PublicationReconciliationEvent.publication_id == publication.id).order_by(PublicationReconciliationEvent.created_at)).all()
    return {
        "publication_id": publication.id, "status": publication.status.value if hasattr(publication.status, "value") else publication.status,
        "approval_id": publication.approval_id, "revision_id": publication.revision_id, "draft_id": publication.draft_id,
        "creative_id": publication.creative_id, "source_image_id": publication.source_image_id,
        "template_id": publication.template_id, "template_key": publication.template_key, "template_version": publication.template_version,
        "text_fingerprint_present": bool(publication.text_fingerprint), "creative_fingerprint_present": bool(publication.creative_fingerprint),
        "pinterest_connection_id": publication.pinterest_connection_id, "pinterest_board_record_id": publication.pinterest_board_record_id,
        "external_board_id": publication.pinterest_board_id_snapshot, "board_name": board.name if board else None,
        "title": publication.title_snapshot, "description": publication.description_snapshot, "alt_text": publication.alt_text_snapshot,
        "destination_url": publication.destination_url, "utm_url": publication.utm_url, "media_url": publication.media_url_snapshot,
        "scheduled_for": publication.scheduled_for, "published_at": publication.published_at, "pinterest_pin_id": publication.pinterest_pin_id,
        "quality": readiness["quality"], "duplicate": readiness["duplicate"], "manual_readiness": {"status": readiness["manual_status"], "ready": readiness["manual_ready"]},
        "provider_readiness": {"status": readiness["provider_status"], "live_provider_write_enabled": readiness["live_provider_write_enabled"]},
        "authorization": readiness["authorization"],
        "attempts": [{"attempt_number": a.attempt_number, "status": a.status, "started_at": a.started_at, "completed_at": a.completed_at, "provider_pin_id": a.provider_pin_id, "error_code": a.error_code, "safe_response_metadata": sanitize_metadata(a.safe_response_metadata)} for a in attempts],
        "reconciliation": [{"id": e.id, "action": e.action, "actor": e.actor, "previous_status": e.previous_status, "new_status": e.new_status, "provider_pin_id": e.provider_pin_id, "reason": e.reason, "created_at": e.created_at, "attempt_id": e.attempt_id} for e in events],
        "checklist": [{"code": "QUALITY_PASS", "passed": readiness["quality"]["status"] == "PASS", "status": readiness["quality"]["status"]}, {"code": "DUPLICATE_SAFE", "passed": readiness["duplicate"]["status"] == "SAFE_TO_CONTINUE", "status": readiness["duplicate"]["status"]}, {"code": "MANUAL_STRUCTURAL_READY", "passed": readiness["manual_ready"], "status": readiness["manual_status"]}, {"code": "DISPATCH_AUTHORIZATION_ACTIVE", "passed": readiness["authorization"]["status"] == "ACTIVE", "status": readiness["authorization"]["status"]}, {"code": "PROVIDER_WRITE_READINESS", "passed": readiness["live_provider_write_enabled"], "status": readiness["provider_status"]}],
        "confirmation_text_version": CONFIRMATION_TEXT_VERSION, "confirmation_prompt": CONFIRMATION_PROMPT,
        "live_publishing_enabled": bool(get_settings().publishing_enabled),
    }
