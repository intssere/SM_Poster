from types import SimpleNamespace
from app.services.publication_preview import CONFIRMATION_PROMPT
from app.services.publication_preview import build_preview
from app.models.domain import PublicationAttempt, PublicationReconciliationEvent
from test_manual_dispatch_authorization import _db, _ready_publication


def test_confirmation_prompt_is_server_owned_and_bounded():
    assert "reviewed this exact approved publication" in CONFIRMATION_PROMPT
    assert "future manual Pinterest dispatch" in CONFIRMATION_PROMPT


def test_preview_service_module_exposes_no_provider_credentials():
    from app.services import publication_preview
    assert not hasattr(publication_preview, "access_token")
    assert not hasattr(publication_preview, "refresh_token")


def test_build_preview_real_db_is_server_derived_and_sanitized():
    db = _db()
    publication = _ready_publication(db)
    db.add(PublicationAttempt(publication_id=publication.id, attempt_number=1, status="UNKNOWN", request_fingerprint="internal-secret", safe_response_metadata={"validated_pin_id": "pin-1", "access_token": "secret", "request_id": "req"}))
    db.commit()
    preview = build_preview(db, publication)
    required = {"publication_id", "status", "approval_id", "revision_id", "draft_id", "creative_id", "source_image_id", "template_id", "template_key", "template_version", "quality", "duplicate", "manual_readiness", "provider_readiness", "authorization", "attempts", "reconciliation", "checklist", "confirmation_text_version", "confirmation_prompt", "live_publishing_enabled"}
    assert required <= preview.keys()
    attempt = preview["attempts"][0]
    assert attempt["safe_response_metadata"] == {"validated_pin_id": "pin-1", "request_id": "req"}
    assert "request_fingerprint" not in attempt
    assert "access_token" not in str(preview)
