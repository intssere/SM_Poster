from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.db.base import Base
from app.models.domain import PinPublication, PinterestBoard, PinterestConnection
from app.services.pinterest_publication_quality import _board_relevance_checks


SYNCED_AT = datetime(2026, 9, 12, 9, 12, 54, tzinfo=timezone.utc)


def _db():
    engine = create_engine("sqlite+pysqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine, expire_on_commit=False)()


def _server_owned_routing():
    db = _db()
    connection = PinterestConnection(
        id="connection-current",
        provider="pinterest",
        external_user_id="diamond-shelf",
        username="diamondshelfllc",
        account_type="BUSINESS",
        access_token_ciphertext="encrypted-access-token",
        refresh_token_ciphertext="encrypted-refresh-token",
        status="CONNECTED",
        boards_last_synced_at=SYNCED_AT,
    )
    board = PinterestBoard(
        id="board-record-current",
        connection_id=connection.id,
        external_board_id="1093811896939213383",
        name="Arabian Perfumes",
        is_active=True,
        is_eligible=True,
        routing_label="Arabian Fragrance Parfum",
        last_synced_at=SYNCED_AT,
    )
    db.add_all([connection, board])
    db.commit()
    publication = PinPublication(
        id="publication-buffer-routing",
        pinterest_connection_id=connection.id,
        pinterest_board_record_id=board.id,
        pinterest_board_id_snapshot=board.external_board_id,
        board_id=None,
    )
    return db, publication, connection, board


def _buffer_board_check(db, publication):
    checks = _board_relevance_checks(db, publication, dispatch_provider="buffer")
    assert len(checks) == 1
    assert checks[0].code == "BUFFER_BOARD_SELECTION_MATCH"
    return checks[0]


def test_buffer_quality_accepts_server_owned_pinterest_board_identity_without_legacy_board_id():
    db, publication, _connection, _board = _server_owned_routing()

    check = _buffer_board_check(db, publication)

    assert check.passed is True
    assert publication.board_id is None


@pytest.mark.parametrize(
    "drift",
    [
        "missing_board_record",
        "missing_connection",
        "disconnected_connection",
        "inactive_board",
        "ineligible_board",
        "missing_board_sync",
        "missing_connection_sync",
        "sync_timestamp_mismatch",
        "external_board_snapshot_mismatch",
    ],
)
def test_buffer_quality_fails_closed_when_server_owned_pinterest_routing_drifts(drift):
    db, publication, connection, board = _server_owned_routing()

    if drift == "missing_board_record":
        publication.pinterest_board_record_id = None
    elif drift == "missing_connection":
        publication.pinterest_connection_id = None
    elif drift == "disconnected_connection":
        connection.status = "DISCONNECTED"
    elif drift == "inactive_board":
        board.is_active = False
    elif drift == "ineligible_board":
        board.is_eligible = False
    elif drift == "missing_board_sync":
        board.last_synced_at = None
    elif drift == "missing_connection_sync":
        connection.boards_last_synced_at = None
    elif drift == "sync_timestamp_mismatch":
        board.last_synced_at = SYNCED_AT + timedelta(seconds=1)
    elif drift == "external_board_snapshot_mismatch":
        publication.pinterest_board_id_snapshot = "different-board"

    db.commit()
    check = _buffer_board_check(db, publication)

    assert check.passed is False
