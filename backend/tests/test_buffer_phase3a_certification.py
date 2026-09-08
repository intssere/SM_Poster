from app.models.domain import Board
import json
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import event, select

from app.core.config import Settings
from app.models.domain import (
    PinApproval,
    PinCreative,
    PinPublication,
    PinterestBoard,
    PinterestConnection,
    ProductImage,
    PublicationAttempt,
    PublicationDispatchAuthorization,
    PublicationReconciliationEvent,
    PublicationStatus,
)
from app.services.buffer_pilot_certification import certify_buffer_pilot_candidate
from app.services.publication_dispatch_authorization import (
    create_authorization,
    manual_structural_readiness,
)
from app.services.publication_duplicates import SAFE_TO_CONTINUE
from app.services.publication_scheduler import request_fingerprint_for
from app.services.pinterest_publisher import normalize_persisted_utc
from test_manual_publication_dispatch import _db, _ready_publication


SECRET = "fake-buffer-phase3a-secret-not-real"
NOW = datetime(2026, 9, 6, 12, 0, tzinfo=timezone.utc)


def settings(**overrides):
    values = {
        "DATABASE_URL": "sqlite+pysqlite:///:memory:",
        "publishing_enabled": False,
        "buffer_publishing_enabled": False,
        "buffer_single_pin_pilot_enabled": False,
        "buffer_api_key": SECRET,
        "buffer_api_base": "https://api.buffer.com",
        "buffer_organization_id": "org",
        "buffer_pinterest_channel_id": "channel",
    }
    values.update(overrides)
    return Settings(_env_file=None, **values)


@pytest.fixture
def case():
    SessionLocal, engine = _db()
    with SessionLocal() as db:
        publication = _ready_publication(db, dispatch_provider="buffer", scopes=["user_accounts:read", "boards:read", "pins:read"])
        yield db, publication
    engine.dispose()


def certify(db, publication, **overrides):
    return certify_buffer_pilot_candidate(
        db,
        publication.id,
        settings=settings(**overrides),
        now=NOW,
    )


def serialized(dossier):
    return json.dumps(dossier, sort_keys=True, default=str)


def assert_secret_absent(dossier):
    text = serialized(dossier)
    assert SECRET not in text
    assert "credential_length" not in text
    assert "credential_prefix" not in text
    assert "credential_suffix" not in text
    assert "credential_hash" not in text
    assert "Bearer " not in text


def test_explicit_publication_id_required_and_no_auto_selection(case):
    db, publication = case
    dossier = certify_buffer_pilot_candidate(db, "", settings=settings(), now=NOW)
    assert dossier["status"] == "PUBLICATION_ID_REQUIRED"
    assert dossier["publication_id"] is None
    assert db.get(PinPublication, publication.id).status == PublicationStatus.SCHEDULED


def test_static_ready_candidate_is_locked_with_false_flags_and_future_schedule(case):
    db, publication = case
    publication.scheduled_for = NOW + timedelta(days=1)
    db.commit()
    dossier = certify(db, publication)
    assert dossier["status"] == "STATIC_CANDIDATE_READY"
    assert dossier["live_execution"] == "LOCKED"
    assert dossier["protected_gates"] == {
        "publishing_enabled": False,
        "buffer_publishing_enabled": False,
        "buffer_single_pin_pilot_enabled": False,
    }
    assert dossier["buffer"]["write_credential_authorized"] is False
    assert dossier["buffer"]["provider_destination_live_verified"] is False
    assert dossier["buffer"]["external_link_format_live_certified"] is False
    assert dossier["media"]["live_fetch_verified"] is False
    assert dossier["duplicate"]["status"] == SAFE_TO_CONTINUE


def test_task39_require_due_default_remains_unchanged(case):
    db, publication = case
    publication.scheduled_for = NOW + timedelta(hours=1)
    db.commit()
    assert manual_structural_readiness(db, publication, dispatch_provider="buffer", now=NOW)["status"] == "NOT_DUE"
    assert manual_structural_readiness(db, publication, dispatch_provider="buffer", now=NOW, require_due=False)["ready"] is True


def test_certification_performs_zero_http_gateway_or_database_mutation(case, monkeypatch):
    db, publication = case
    calls = []

    def fail(*args, **kwargs):
        calls.append((args, kwargs))
        raise AssertionError("network/gateway construction is forbidden")

    monkeypatch.setattr("httpx.AsyncClient", fail)
    monkeypatch.setattr("app.integrations.buffer.gateway.BufferGateway", fail)
    monkeypatch.setattr("app.integrations.pinterest.gateway.PinterestV5Gateway", fail)
    before = {
        "publication": tuple(db.execute(select(PinPublication.id, PinPublication.status, PinPublication.error_code)).all()),
        "attempts": db.query(PublicationAttempt).count(),
        "auth": db.query(PublicationDispatchAuthorization).count(),
    }
    flushes = []
    event.listen(db, "before_flush", lambda *args: flushes.append("flush"))
    dossier = certify(db, publication)
    after = {
        "publication": tuple(db.execute(select(PinPublication.id, PinPublication.status, PinPublication.error_code)).all()),
        "attempts": db.query(PublicationAttempt).count(),
        "auth": db.query(PublicationDispatchAuthorization).count(),
    }
    assert dossier["status"] == "STATIC_CANDIDATE_READY"
    assert calls == []
    assert flushes == []
    assert before == after


def test_certification_no_autoflush_preserves_unrelated_dirty_object():
    SessionLocal, engine = _db()
    db = SessionLocal()
    audit = SessionLocal()
    try:
        publication = _ready_publication(db, dispatch_provider="buffer", scopes=["user_accounts:read", "boards:read", "pins:read"])
        source = db.get(ProductImage, publication.source_image_id)
        source.alt_text = "dirty unrelated pending change"
        flushes = []

        def before_flush(*args):
            flushes.append("flush")

        event.listen(db, "before_flush", before_flush)
        first = certify(db, publication)
        second = certify(db, publication)
        persisted_source = audit.get(ProductImage, publication.source_image_id)
        assert first == second
        assert first["status"] == "STATIC_CANDIDATE_READY"
        assert flushes == []
        assert persisted_source.alt_text is None
        event.remove(db, "before_flush", before_flush)
    finally:
        db.close()
        audit.close()
        engine.dispose()


@pytest.mark.parametrize(
    "status",
    [
        PublicationStatus.PUBLISHED,
        PublicationStatus.PUBLISH_UNKNOWN,
        PublicationStatus.PUBLISHING,
        PublicationStatus.CANCELLED,
    ],
)
def test_non_scheduled_publication_states_are_blocked(case, status):
    db, publication = case
    publication.status = status
    db.commit()
    assert certify(db, publication)["status"] == "INVALID_PUBLICATION_STATE"


def test_prior_attempt_and_known_pin_are_blocked(case):
    db, publication = case
    db.add(PublicationAttempt(publication_id=publication.id, attempt_number=1, status="FAILED"))
    db.commit()
    assert certify(db, publication)["status"] == "PRIOR_ATTEMPT_EXISTS"
    db.query(PublicationAttempt).delete()
    publication.pinterest_pin_id = "123"
    db.commit()
    assert certify(db, publication)["status"] == "KNOWN_PIN_EXISTS"


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("approval_decision", "APPROVAL_INVALID"),
        ("approval_revision", "APPROVAL_INVALID"),
        ("approval_creative", "APPROVAL_INVALID"),
        ("creative_source", "CREATIVE_INVALID"),
        ("missing_source", "SOURCE_IMAGE_INVALID"),
        ("board_external", "DESTINATION_INVALID"),
        ("connection_status", "DESTINATION_INVALID"),
        ("incomplete_snapshot", "INCOMPLETE_SNAPSHOT"),
    ],
)
def test_static_certification_blocks_identity_and_destination_drift(case, mutation, expected):
    db, publication = case
    if mutation == "approval_decision":
        db.get(PinApproval, publication.approval_id).decision = "REJECTED"
    elif mutation == "approval_revision":
        db.get(PinApproval, publication.approval_id).revision_id = "other-revision"
    elif mutation == "approval_creative":
        db.get(PinApproval, publication.approval_id).creative_id = "other-creative"
    elif mutation == "creative_source":
        db.get(PinCreative, publication.creative_id).source_image_id = "other-source"
    elif mutation == "missing_source":
        db.delete(db.get(ProductImage, publication.source_image_id))
    elif mutation == "board_external":
        db.get(Board, publication.board_id).pinterest_board_id = "other-board"
    elif mutation == "connection_status":
        db.get(Board, publication.board_id).active = False
    elif mutation == "incomplete_snapshot":
        publication.title_snapshot = None
    db.commit()
    assert certify(db, publication)["status"] == expected


@pytest.mark.parametrize(
    "mutation,expected",
    [
        ("quality_fail", "QUALITY_FAILED"),
        ("quality_warning", "QUALITY_WARNING"),
        ("duplicate", "DUPLICATE_PUBLICATION"),
        ("media_invalid", "MEDIA_NOT_PUBLISHABLE"),
        ("utm_mismatch", "QUALITY_FAILED"),
        ("missing_org", "BUFFER_ORGANIZATION_REQUIRED"),
        ("missing_channel", "BUFFER_CHANNEL_REQUIRED"),
        ("invalid_base", "BUFFER_API_BASE_INVALID"),
    ],
)
def test_static_certification_blocks_policy_duplicates_media_and_buffer_config(case, mutation, expected):
    db, publication = case
    overrides = {}
    if mutation == "quality_fail":
        publication.title_snapshot = "x" * 101
    elif mutation == "quality_warning":
        creative = db.get(PinCreative, publication.creative_id)
        creative.width = 1000
        creative.height = 1000
    elif mutation == "duplicate":
        other = PinPublication(
            id="other-publication",
            draft_id=publication.draft_id,
            creative_id=publication.creative_id,
            publication_fingerprint="q" * 64,
            status=PublicationStatus.PUBLISHED,
            pinterest_board_id_snapshot=publication.pinterest_board_id_snapshot,
            utm_url=publication.utm_url,
            creative_fingerprint=publication.creative_fingerprint,
            text_fingerprint=publication.text_fingerprint,
        )
        db.add(other)
    elif mutation == "media_invalid":
        publication.media_url_snapshot = "http://localhost/image.jpg"
        db.get(PinCreative, publication.creative_id).rendered_url = publication.media_url_snapshot
    elif mutation == "utm_mismatch":
        publication.utm_url = "https://diamondshelf.us/products/other?utm_source=pinterest"
    elif mutation == "missing_org":
        overrides["buffer_organization_id"] = None
    elif mutation == "missing_channel":
        overrides["buffer_pinterest_channel_id"] = None
    elif mutation == "invalid_base":
        overrides["buffer_api_base"] = "https://evil.invalid"
    db.commit()
    assert certify(db, publication, **overrides)["status"] == expected


@pytest.mark.parametrize(
    "overrides,expected,field",
    [
        ({"buffer_organization_id": None}, "BUFFER_ORGANIZATION_REQUIRED", "organization_configured"),
        ({"buffer_organization_id": ""}, "BUFFER_ORGANIZATION_REQUIRED", "organization_configured"),
        ({"buffer_organization_id": "   "}, "BUFFER_ORGANIZATION_REQUIRED", "organization_configured"),
        ({"buffer_organization_id": "org bad"}, "BUFFER_ORGANIZATION_REQUIRED", "organization_configured"),
        ({"buffer_organization_id": "org.bad"}, "BUFFER_ORGANIZATION_REQUIRED", "organization_configured"),
        ({"buffer_pinterest_channel_id": None}, "BUFFER_CHANNEL_REQUIRED", "channel_configured"),
        ({"buffer_pinterest_channel_id": ""}, "BUFFER_CHANNEL_REQUIRED", "channel_configured"),
        ({"buffer_pinterest_channel_id": "   "}, "BUFFER_CHANNEL_REQUIRED", "channel_configured"),
        ({"buffer_pinterest_channel_id": "channel bad"}, "BUFFER_CHANNEL_REQUIRED", "channel_configured"),
        ({"buffer_pinterest_channel_id": "channel.bad"}, "BUFFER_CHANNEL_REQUIRED", "channel_configured"),
    ],
)
def test_buffer_identifier_structural_validation(case, overrides, expected, field):
    db, publication = case
    dossier = certify(db, publication, **overrides)
    assert dossier["status"] == expected
    assert dossier["buffer"][field] is False


def test_valid_buffer_identifiers_still_certify(case):
    db, publication = case
    dossier = certify(
        db,
        publication,
        buffer_organization_id="org_123-ABC",
        buffer_pinterest_channel_id="channel_456-XYZ",
    )
    assert dossier["status"] == "STATIC_CANDIDATE_READY"
    assert dossier["buffer"]["organization_configured"] is True
    assert dossier["buffer"]["channel_configured"] is True


def test_reconciliation_history_blocks_static_certification(case):
    db, publication = case
    db.add(
        PublicationReconciliationEvent(
            publication_id=publication.id,
            actor="operator",
            action="PROVIDER_PIN_CONFIRMED",
            previous_status="PUBLISH_UNKNOWN",
            new_status="PUBLISHED",
            provider="buffer",
            provider_pin_id="123456789",
            reason="historical reconciliation",
        )
    )
    db.commit()
    dossier = certify(db, publication)
    assert dossier["status"] == "RECONCILIATION_HISTORY_EXISTS"
    assert dossier["reconciliation"] == {"count": 1}
    assert "historical reconciliation" not in serialized(dossier)


@pytest.mark.parametrize("status", ["ACTIVE", "EXPIRED", "REVOKED", "CONSUMED"])
def test_authorization_status_is_safe_and_nonblocking(case, status):
    db, publication = case
    auth = create_authorization(db, publication, dispatch_provider="buffer", actor="operator")
    if status == "EXPIRED":
        auth.expires_at = NOW - timedelta(minutes=1)
    elif status != "ACTIVE":
        auth.status = status
    db.commit()
    before = (
        auth.status,
        normalize_persisted_utc(auth.expires_at),
        normalize_persisted_utc(auth.consumed_at),
        normalize_persisted_utc(auth.revoked_at),
    )
    dossier = certify(db, publication)
    db.refresh(auth)
    assert dossier["status"] == "STATIC_CANDIDATE_READY"
    assert dossier["authorization"]["status"] == status
    assert (
        auth.status,
        normalize_persisted_utc(auth.expires_at),
        normalize_persisted_utc(auth.consumed_at),
        normalize_persisted_utc(auth.revoked_at),
    ) == before


def test_no_authorization_does_not_block_static_certification(case):
    db, publication = case
    dossier = certify(db, publication)
    assert dossier["status"] == "STATIC_CANDIDATE_READY"
    assert dossier["authorization"]["status"] == "NOT_CREATED"


def test_dossier_contains_exact_fingerprints_payload_and_is_deterministic(case):
    db, publication = case
    first = certify(db, publication)
    second = certify(db, publication)
    assert first == second
    assert first["publication_fingerprint"] == publication.publication_fingerprint
    assert first["request_fingerprint"] == request_fingerprint_for(publication)
    assert first["payload"] == {
        "title": publication.title_snapshot,
        "description": publication.description_snapshot,
        "alt_text": publication.alt_text_snapshot,
        "destination_url": publication.destination_url,
        "utm_url": publication.utm_url,
        "media_url": publication.media_url_snapshot,
    }


def test_buffer_credential_is_boolean_only_and_secret_never_serializes(case):
    db, publication = case
    with_secret = certify(db, publication, buffer_api_key=SECRET)
    without_secret = certify(db, publication, buffer_api_key="   ")
    assert with_secret["buffer"]["credential_configured"] is True
    assert without_secret["buffer"]["credential_configured"] is False
    assert_secret_absent(with_secret)
    assert_secret_absent(without_secret)
