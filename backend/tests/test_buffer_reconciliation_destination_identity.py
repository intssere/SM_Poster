import asyncio
from datetime import datetime, timedelta, timezone
from types import SimpleNamespace

import pytest
from sqlalchemy import select

from app.integrations.buffer.gateway import BufferPostSnapshot
from app.models.domain import (
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


def _pilot4_shaped_case(tmp_path):
    Session, engine = _db(tmp_path / "buffer-reconcile-destination.db")
    db = Session()
    publication = _ready_publication(db, dispatch_provider="buffer")
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
    return db, engine, publication, attempt, connection, board


def _run(db, publication, gateway):
    return asyncio.run(reconcile_buffer(
        db,
        publication.id,
        actor="operator",
        settings=SETTINGS,
        gateway=gateway,
    ))


def test_pilot4_shaped_server_owned_destination_reconciles_exact_sent_post(tmp_path):
    db, engine, publication, attempt, connection, board = _pilot4_shaped_case(tmp_path)
    try:
        gateway = ExactGateway(publication)
        result = _run(db, publication, gateway)
        db.refresh(attempt)

        assert gateway.calls == [PILOT4_OPERATION]
        assert result.status == PublicationStatus.PUBLISHED
        assert result.error_code is None
        assert result.pinterest_pin_id == PILOT4_PIN
        assert result.pinterest_connection_id == connection.id
        assert result.pinterest_board_record_id == board.id
        assert attempt.status == "SUCCEEDED"
        assert attempt.error_code is None
        assert attempt.provider_operation_id == PILOT4_OPERATION
        assert attempt.provider_operation_status == "sent"
        assert attempt.provider_pin_id == PILOT4_PIN
        assert attempt.completed_at is not None

        events = db.scalars(select(PublicationReconciliationEvent).where(
            PublicationReconciliationEvent.publication_id == publication.id
        )).all()
        assert len(events) == 1
        event = events[0]
        assert event.action == "PROVIDER_PIN_CONFIRMED"
        assert event.previous_status == "PUBLISH_UNKNOWN"
        assert event.new_status == "PUBLISHED"
        assert event.provider == "buffer"
        assert event.provider_operation_id == PILOT4_OPERATION
        assert event.provider_pin_id == PILOT4_PIN
    finally:
        db.close()
        engine.dispose()


@pytest.mark.parametrize(
    "drift",
    [
        "partial_identity",
        "external_board",
        "connection_relation",
        "connection_status",
        "connection_provider",
        "sync_identity",
    ],
)
def test_server_owned_destination_drift_fails_before_exact_provider_read(tmp_path, drift):
    db, engine, publication, attempt, connection, board = _pilot4_shaped_case(tmp_path)
    try:
        if drift == "partial_identity":
            publication.pinterest_board_record_id = None
        elif drift == "external_board":
            board.external_board_id = "different-board"
        elif drift == "connection_relation":
            other = PinterestConnection(
                id="other-connection",
                provider="pinterest",
                external_user_id="other-user",
                granted_scopes=["pins:read"],
                access_token_ciphertext="other-access",
                refresh_token_ciphertext="other-refresh",
                status="CONNECTED",
                boards_last_synced_at=connection.boards_last_synced_at,
            )
            db.add(other)
            board.connection_id = other.id
        elif drift == "connection_status":
            connection.status = "DISCONNECTED"
        elif drift == "connection_provider":
            connection.provider = "other"
        else:
            board.last_synced_at = connection.boards_last_synced_at + timedelta(seconds=1)
        db.commit()

        gateway = ExactGateway(publication)
        with pytest.raises(BufferReconciliationError, match="^BUFFER_POST_SNAPSHOT_MISMATCH$"):
            _run(db, publication, gateway)

        assert gateway.calls == []
        db.refresh(publication)
        db.refresh(attempt)
        assert publication.status == PublicationStatus.PUBLISH_UNKNOWN
        assert publication.pinterest_pin_id is None
        assert attempt.status == "UNKNOWN"
        assert attempt.provider_pin_id is None
        assert db.scalars(select(PublicationReconciliationEvent).where(
            PublicationReconciliationEvent.publication_id == publication.id
        )).all() == []
    finally:
        db.close()
        engine.dispose()


def test_legacy_destination_without_server_owned_identity_remains_supported(tmp_path):
    db, engine, publication, attempt, _connection, _board = _pilot4_shaped_case(tmp_path)
    try:
        publication.pinterest_connection_id = None
        publication.pinterest_board_record_id = None
        db.commit()
        gateway = ExactGateway(publication)

        result = _run(db, publication, gateway)
        db.refresh(attempt)

        assert gateway.calls == [PILOT4_OPERATION]
        assert result.status == PublicationStatus.PUBLISHED
        assert result.pinterest_pin_id == PILOT4_PIN
        assert attempt.status == "SUCCEEDED"
        assert attempt.provider_pin_id == PILOT4_PIN
    finally:
        db.close()
        engine.dispose()
