import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.integrations.buffer.gateway import BufferPostSnapshot
from app.models.domain import (
    ContentRevision,
    PinApproval,
    PinCreative,
    PinDraft,
    PinterestBoard,
    PinterestConnection,
    PublicationAttempt,
    PublicationReconciliationEvent,
    PublicationStatus,
)
from app.services.buffer_publication_reconciliation import BufferReconciliationError, reconcile_buffer
from app.services.publication_scheduler import request_fingerprint_for
from test_manual_publication_dispatch import _db, _ready_publication


PILOT4_OPERATION = "6aa716b357098b213383cd4f"
PILOT4_PIN = "1093811828279724056"
SETTINGS = SimpleNamespace(buffer_organization_id="org", buffer_pinterest_channel_id="channel")


class ExactGateway:
    """Exact-read-only fake. It deliberately exposes no create/dispatch method."""

    def __init__(self, publication):
        self.publication = publication
        self.calls = []

    async def post(self, operation_id):
        self.calls.append(operation_id)
        return BufferPostSnapshot(
            buffer_post_id=operation_id,
            status="sent",
            channel_id="channel",
            created_at="2026-09-13T21:33:39.455Z",
            due_at="2026-09-13T21:33:39.529Z",
            sent_at="2026-09-13T21:33:44.453Z",
            external_link=f"https://www.pinterest.com/pin/{PILOT4_PIN}",
            channel_service="pinterest",
            text=self.publication.description_snapshot,
            pinterest_board_service_id=self.publication.pinterest_board_id_snapshot,
            pinterest_title=self.publication.title_snapshot,
            pinterest_url=self.publication.utm_url,
            image_url=self.publication.media_url_snapshot,
            image_alt_text=self.publication.alt_text_snapshot,
        )


def _revision(publication):
    return ContentRevision(
        id="pilot4-revision",
        draft_id=publication.draft_id,
        version=2,
        revision_kind="MANUAL",
        status="REVIEW",
        headline="Revised fragrance headline",
        title="Revised fragrance title",
        description="Revised exact approved fragrance description.",
        alt_text="Revised exact approved fragrance creative.",
        cta="Shop now",
        content_angle="Fragrance",
        content_angle_key="fragrance",
        creative_template=publication.template_key,
        creative_template_key=publication.template_key,
        destination_url="https://diamondshelf.us/products/revised-fragrance",
        utm_url="https://diamondshelf.us/products/revised-fragrance?utm_source=pinterest",
        keywords=[],
        facts_used={},
        warnings=[],
        missing_facts=[],
        unsupported_claims=[],
        provenance={},
        text_fingerprint="r" * 64,
        creative_fingerprint=publication.creative_fingerprint,
        creative_id=publication.creative_id,
        source_image_id=publication.source_image_id,
        provider_mode="disabled",
        generation_mode="manual",
        reason="test revision",
        generation_type="copy",
        intended_channel="pinterest",
    )


def _pilot4_case(tmp_path, *, revised=False):
    Session, engine = _db(tmp_path / ("buffer-reconcile-revised.db" if revised else "buffer-reconcile-original.db"))
    db = Session()
    publication = _ready_publication(db, dispatch_provider="buffer")
    legacy_board_id = publication.board_id
    approval = db.get(PinApproval, publication.approval_id)
    draft = db.get(PinDraft, publication.draft_id)
    creative = db.get(PinCreative, publication.creative_id)

    # True Pilot #4 shape: authoritative server-owned destination, original version,
    # and no legacy Board identity persisted on the publication.
    publication.board_id = None
    publication.revision_id = None
    approval.revision_id = None
    approval.approved_version_id = "original"

    revision = None
    if revised:
        revision = _revision(publication)
        db.add(revision)
        publication.revision_id = revision.id
        publication.title_snapshot = revision.title
        publication.description_snapshot = revision.description
        publication.alt_text_snapshot = revision.alt_text
        publication.destination_url = revision.destination_url
        publication.utm_url = revision.utm_url
        publication.text_fingerprint = revision.text_fingerprint
        approval.revision_id = revision.id
        approval.approved_version_id = revision.id

    publication.status = PublicationStatus.PUBLISH_UNKNOWN
    publication.error_code = "BUFFER_SENT_LINK_UNVERIFIED"
    now = datetime.now(timezone.utc)
    connection = PinterestConnection(
        id="pilot4-connection",
        provider="pinterest",
        external_user_id="pilot4-user",
        granted_scopes=["pins:read", "boards:read"],
        access_token_ciphertext="test-access",
        refresh_token_ciphertext="test-refresh",
        status="CONNECTED",
        boards_last_synced_at=now,
    )
    board = PinterestBoard(
        id="pilot4-board-record",
        connection_id=connection.id,
        external_board_id=publication.pinterest_board_id_snapshot,
        name="Arabian Perfumes",
        is_active=True,
        is_eligible=True,
        last_synced_at=now,
    )
    db.add_all([connection, board])
    publication.pinterest_connection_id = connection.id
    publication.pinterest_board_record_id = board.id
    db.flush()
    attempt = PublicationAttempt(
        publication_id=publication.id,
        attempt_number=1,
        status="UNKNOWN",
        dispatch_provider="buffer",
        request_fingerprint=request_fingerprint_for(publication),
        error_code="BUFFER_SENT_LINK_UNVERIFIED",
        provider_operation_id=PILOT4_OPERATION,
        provider_operation_status="sent",
        provider_external_link=f"https://www.pinterest.com/pin/{PILOT4_PIN}",
        provider_submitted_at=now,
        provider_last_observed_at=now,
        safe_response_metadata={"buffer_organization_id": "org", "buffer_channel_id": "channel"},
    )
    db.add(attempt)
    db.commit()
    db.refresh(publication)
    db.refresh(attempt)
    return SimpleNamespace(
        db=db,
        engine=engine,
        publication=publication,
        attempt=attempt,
        approval=approval,
        draft=draft,
        creative=creative,
        revision=revision,
        connection=connection,
        board=board,
        legacy_board_id=legacy_board_id,
    )


def _run(case, gateway):
    return asyncio.run(reconcile_buffer(
        case.db,
        case.publication.id,
        actor="operator",
        settings=SETTINGS,
        gateway=gateway,
    ))


def _assert_pre_provider_rejection(case):
    gateway = ExactGateway(case.publication)
    with pytest.raises(BufferReconciliationError, match="^BUFFER_POST_SNAPSHOT_MISMATCH$"):
        _run(case, gateway)
    assert gateway.calls == []
    case.db.refresh(case.publication)
    case.db.refresh(case.attempt)
    assert case.publication.status == PublicationStatus.PUBLISH_UNKNOWN
    assert case.publication.pinterest_pin_id is None
    assert case.attempt.status == "UNKNOWN"
    assert case.attempt.provider_pin_id is None
    assert case.db.scalars(select(PublicationReconciliationEvent).where(
        PublicationReconciliationEvent.publication_id == case.publication.id
    )).all() == []


def test_true_pilot4_original_modern_destination_reconciles_exact_sent_post_once(tmp_path):
    case = _pilot4_case(tmp_path)
    try:
        assert case.publication.board_id is None
        assert case.publication.revision_id is None
        assert case.approval.approved_version_id == "original"
        gateway = ExactGateway(case.publication)
        result = _run(case, gateway)
        case.db.refresh(case.attempt)

        assert gateway.calls == [PILOT4_OPERATION]
        assert result.status == PublicationStatus.PUBLISHED
        assert result.error_code is None
        assert result.pinterest_pin_id == PILOT4_PIN
        assert result.pinterest_connection_id == case.connection.id
        assert result.pinterest_board_record_id == case.board.id
        assert case.attempt.status == "SUCCEEDED"
        assert case.attempt.provider_operation_id == PILOT4_OPERATION
        assert case.attempt.provider_operation_status == "sent"
        assert case.attempt.provider_pin_id == PILOT4_PIN
        assert case.attempt.completed_at is not None

        events = case.db.scalars(select(PublicationReconciliationEvent).where(
            PublicationReconciliationEvent.publication_id == case.publication.id
        )).all()
        assert len(events) == 1
        assert events[0].provider_operation_id == PILOT4_OPERATION
        assert events[0].provider_pin_id == PILOT4_PIN

        # Terminal reconciliation is one-shot. A second invocation is rejected
        # before another provider read and can never become a retry/dispatch.
        second_gateway = ExactGateway(case.publication)
        with pytest.raises(BufferReconciliationError, match="RECONCILIATION_REQUIRES_PUBLISH_UNKNOWN"):
            _run(case, second_gateway)
        assert second_gateway.calls == []
    finally:
        case.db.close()
        case.engine.dispose()


def test_modern_revised_version_reconciles_when_approval_and_revision_match(tmp_path):
    case = _pilot4_case(tmp_path, revised=True)
    try:
        gateway = ExactGateway(case.publication)
        result = _run(case, gateway)
        assert gateway.calls == [PILOT4_OPERATION]
        assert result.status == PublicationStatus.PUBLISHED
        assert result.pinterest_pin_id == PILOT4_PIN
    finally:
        case.db.close()
        case.engine.dispose()


@pytest.mark.parametrize(
    "drift",
    [
        "connection_only",
        "board_only",
        "connection_missing",
        "board_missing",
        "external_board",
        "connection_relation",
        "connection_status",
        "connection_provider",
        "board_inactive",
        "board_ineligible",
        "board_sync_missing",
        "connection_sync_missing",
        "sync_identity",
    ],
)
def test_modern_destination_drift_fails_before_exact_provider_read(tmp_path, drift):
    case = _pilot4_case(tmp_path)
    try:
        if drift == "connection_only":
            case.publication.pinterest_board_record_id = None
        elif drift == "board_only":
            case.publication.pinterest_connection_id = None
        elif drift == "connection_missing":
            case.db.delete(case.connection)
        elif drift == "board_missing":
            case.db.delete(case.board)
        elif drift == "external_board":
            case.board.external_board_id = "different-board"
        elif drift == "connection_relation":
            case.board.connection_id = "other-connection"
        elif drift == "connection_status":
            case.connection.status = "DISCONNECTED"
        elif drift == "connection_provider":
            case.connection.provider = "other"
        elif drift == "board_inactive":
            case.board.is_active = False
        elif drift == "board_ineligible":
            case.board.is_eligible = False
        elif drift == "board_sync_missing":
            case.board.last_synced_at = None
        elif drift == "connection_sync_missing":
            case.connection.boards_last_synced_at = None
        else:
            case.board.last_synced_at = case.connection.boards_last_synced_at + timedelta(seconds=1)
        case.db.commit()
        _assert_pre_provider_rejection(case)
    finally:
        case.db.close()
        case.engine.dispose()


def test_invalid_modern_identity_never_falls_back_to_valid_legacy_route(tmp_path):
    case = _pilot4_case(tmp_path)
    try:
        # Even if a valid historical Board/concept route is reattached, the
        # presence of modern IDs selects modern mode exclusively.
        case.publication.board_id = case.legacy_board_id
        case.board.external_board_id = "drifted-modern-board"
        case.db.commit()
        _assert_pre_provider_rejection(case)
    finally:
        case.db.close()
        case.engine.dispose()


@pytest.mark.parametrize(
    "drift",
    [
        "approval_missing",
        "approval_rejected",
        "approval_draft",
        "approval_creative",
        "approval_revision",
        "approved_version",
        "draft_missing",
        "title",
        "description",
        "alt_text",
        "destination",
        "utm",
        "text_fingerprint",
        "creative_missing",
        "creative_draft",
        "source_image",
        "template",
        "creative_fingerprint",
        "template_key",
        "media_snapshot",
    ],
)
def test_original_approval_and_creative_provenance_drift_fails_before_provider_read(tmp_path, drift):
    case = _pilot4_case(tmp_path)
    try:
        if drift == "approval_missing":
            case.db.delete(case.approval)
        elif drift == "approval_rejected":
            case.approval.decision = "REJECTED"
        elif drift == "approval_draft":
            case.approval.draft_id = "other-draft"
        elif drift == "approval_creative":
            case.approval.creative_id = "other-creative"
        elif drift == "approval_revision":
            case.approval.revision_id = "unexpected-revision"
        elif drift == "approved_version":
            case.approval.approved_version_id = "not-original"
        elif drift == "draft_missing":
            case.db.delete(case.draft)
        elif drift == "title":
            case.draft.title = "drifted title"
        elif drift == "description":
            case.draft.description = "drifted description"
        elif drift == "alt_text":
            case.draft.alt_text = "drifted alt"
        elif drift == "destination":
            case.draft.destination_url = "https://diamondshelf.us/drift"
        elif drift == "utm":
            case.draft.utm_url = "https://diamondshelf.us/drift?utm_source=pinterest"
        elif drift == "text_fingerprint":
            case.draft.text_fingerprint = "x" * 64
        elif drift == "creative_missing":
            case.db.delete(case.creative)
        elif drift == "creative_draft":
            case.creative.draft_id = "other-draft"
        elif drift == "source_image":
            case.creative.source_image_id = "other-source"
        elif drift == "template":
            case.creative.template_id = "other-template"
        elif drift == "creative_fingerprint":
            case.creative.creative_fingerprint = "z" * 64
        elif drift == "template_key":
            case.publication.template_key = "other-template-key"
        else:
            case.publication.media_url_snapshot = None
        case.db.commit()
        _assert_pre_provider_rejection(case)
    finally:
        case.db.close()
        case.engine.dispose()


@pytest.mark.parametrize(
    "drift",
    [
        "revision_missing",
        "revision_draft",
        "approval_revision",
        "approved_version",
        "revision_creative",
        "revision_title",
        "revision_destination",
        "revision_text_fingerprint",
    ],
)
def test_revised_version_provenance_drift_fails_before_provider_read(tmp_path, drift):
    case = _pilot4_case(tmp_path, revised=True)
    try:
        if drift == "revision_missing":
            case.db.delete(case.revision)
        elif drift == "revision_draft":
            case.revision.draft_id = "other-draft"
        elif drift == "approval_revision":
            case.approval.revision_id = "other-revision"
        elif drift == "approved_version":
            case.approval.approved_version_id = "other-version"
        elif drift == "revision_creative":
            case.revision.creative_id = "other-creative"
        elif drift == "revision_title":
            case.revision.title = "drifted revised title"
        elif drift == "revision_destination":
            case.revision.destination_url = "https://diamondshelf.us/drifted-revision"
        else:
            case.revision.text_fingerprint = "x" * 64
        case.db.commit()
        _assert_pre_provider_rejection(case)
    finally:
        case.db.close()
        case.engine.dispose()


def test_legacy_destination_without_server_owned_identity_remains_supported(tmp_path):
    case = _pilot4_case(tmp_path)
    try:
        case.publication.pinterest_connection_id = None
        case.publication.pinterest_board_record_id = None
        case.publication.board_id = case.legacy_board_id
        case.db.commit()
        case.attempt.request_fingerprint = request_fingerprint_for(case.publication)
        case.db.commit()
        gateway = ExactGateway(case.publication)

        result = _run(case, gateway)
        case.db.refresh(case.attempt)

        assert gateway.calls == [PILOT4_OPERATION]
        assert result.status == PublicationStatus.PUBLISHED
        assert result.pinterest_pin_id == PILOT4_PIN
        assert case.attempt.status == "SUCCEEDED"
        assert case.attempt.provider_pin_id == PILOT4_PIN
    finally:
        case.db.close()
        case.engine.dispose()
