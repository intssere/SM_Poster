import asyncio

import pytest

from app.api.routes import buffer_reconciliation as reconciliation_route
from app.integrations.buffer.gateway import BufferPostSnapshot, BufferReadError
from app.models.domain import Board, PublicationAttempt, PublicationStatus
from app.services import buffer_publication_reconciliation as reconciliation_service
from app.services.buffer_publication_reconciliation import (
    BufferReconciliationError,
    RECONCILIATION_RECEIPTS_KEY,
    attest_buffer_reconciliation_receipt,
    reconcile_buffer,
)
from test_buffer_reconciliation_destination_identity import (
    ExactGateway,
    PILOT4_OPERATION,
    PILOT4_PIN,
    SETTINGS,
    _historical_phase_c_case,
)


def _run(case, gateway):
    return asyncio.run(
        reconcile_buffer(
            case.db,
            case.publication.id,
            actor="operator",
            settings=SETTINGS,
            gateway=gateway,
        )
    )


def test_receipt_route_requires_auth_and_delegates(monkeypatch):
    calls = []
    expected = {
        "provider_free": True,
        "read_only": True,
        "receipt_present": False,
        "invocation_count": 0,
        "sequence": None,
        "phase": None,
        "provider_read_started": False,
        "provider_read_completed": False,
        "provider_operation_status": None,
        "fallback_selected": False,
        "code": None,
        "stage": None,
        "field": None,
    }

    def fake_attest(db, publication_id):
        calls.append((db, publication_id))
        return expected

    monkeypatch.setattr(
        reconciliation_route,
        "attest_buffer_reconciliation_receipt",
        fake_attest,
    )
    monkeypatch.setattr(reconciliation_route, "current_user", lambda request: None)

    with pytest.raises(Exception) as error:
        reconciliation_route.attest_known_buffer_operation_receipt(
            "publication",
            object(),
            object(),
        )
    assert getattr(error.value, "status_code", None) == 401
    assert calls == []

    db = object()
    monkeypatch.setattr(
        reconciliation_route,
        "current_user",
        lambda request: "operator@example.test",
    )
    observed = reconciliation_route.attest_known_buffer_operation_receipt(
        "publication",
        object(),
        db,
    )
    assert observed == expected
    assert calls == [(db, "publication")]


def test_receipt_records_pre_provider_rejection_without_provider_read(tmp_path):
    case = _historical_phase_c_case(tmp_path)
    try:
        local_board = case.db.get(Board, case.legacy_board_id)
        local_board.pinterest_board_id = "different-external-board"
        case.db.commit()
        gateway = ExactGateway(case.publication)

        with pytest.raises(BufferReconciliationError, match="^BUFFER_POST_SNAPSHOT_MISMATCH$"):
            _run(case, gateway)

        assert gateway.calls == []
        receipt = attest_buffer_reconciliation_receipt(case.db, case.publication.id)
        assert receipt["receipt_present"] is True
        assert receipt["invocation_count"] == 1
        assert receipt["phase"] == "PRE_PROVIDER_REJECTED"
        assert receipt["provider_read_started"] is False
        assert receipt["provider_read_completed"] is False
        assert receipt["code"] == "BUFFER_POST_SNAPSHOT_MISMATCH"
        assert receipt["stage"] == "pre_provider"
        assert receipt["field"] == "destination_identity"
    finally:
        case.db.close()
        case.engine.dispose()


def test_receipt_records_provider_read_failure_exactly_once(tmp_path):
    case = _historical_phase_c_case(tmp_path)
    try:
        class FailingGateway:
            def __init__(self):
                self.calls = []

            async def post(self, operation_id):
                self.calls.append(operation_id)
                raise BufferReadError("safe-test-failure")

        gateway = FailingGateway()
        with pytest.raises(BufferReconciliationError, match="^BUFFER_RECONCILIATION_READ_FAILED$"):
            _run(case, gateway)

        assert gateway.calls == [PILOT4_OPERATION]
        receipt = attest_buffer_reconciliation_receipt(case.db, case.publication.id)
        assert receipt["phase"] == "PROVIDER_READ_FAILED"
        assert receipt["provider_read_started"] is True
        assert receipt["provider_read_completed"] is False
        assert receipt["code"] == "BUFFER_RECONCILIATION_READ_FAILED"
        case.db.refresh(case.publication)
        case.db.refresh(case.attempt)
        assert case.publication.status == PublicationStatus.PUBLISH_UNKNOWN
        assert case.attempt.status == "UNKNOWN"
    finally:
        case.db.close()
        case.engine.dispose()


def test_receipt_records_post_provider_rejection_after_one_exact_read(tmp_path):
    case = _historical_phase_c_case(tmp_path)
    try:
        class MismatchGateway(ExactGateway):
            async def post(self, operation_id):
                snapshot = await super().post(operation_id)
                return BufferPostSnapshot(
                    buffer_post_id=snapshot.buffer_post_id,
                    status=snapshot.status,
                    channel_id=snapshot.channel_id,
                    created_at=snapshot.created_at,
                    due_at=snapshot.due_at,
                    sent_at=snapshot.sent_at,
                    external_link=snapshot.external_link,
                    channel_service=snapshot.channel_service,
                    text=snapshot.text,
                    pinterest_board_service_id=snapshot.pinterest_board_service_id,
                    pinterest_title="different-title",
                    pinterest_url=snapshot.pinterest_url,
                    image_url=snapshot.image_url,
                    image_alt_text=snapshot.image_alt_text,
                )

        gateway = MismatchGateway(case.publication)
        with pytest.raises(BufferReconciliationError, match="^BUFFER_POST_SNAPSHOT_MISMATCH$"):
            _run(case, gateway)

        assert gateway.calls == [PILOT4_OPERATION]
        receipt = attest_buffer_reconciliation_receipt(case.db, case.publication.id)
        assert receipt["phase"] == "POST_PROVIDER_REJECTED"
        assert receipt["provider_read_started"] is True
        assert receipt["provider_read_completed"] is True
        assert receipt["provider_operation_status"] == "sent"
        assert receipt["code"] == "BUFFER_POST_SNAPSHOT_MISMATCH"
        assert receipt["stage"] == "provider_snapshot"
        assert receipt["field"] == "title"
    finally:
        case.db.close()
        case.engine.dispose()


def test_historical_fallback_receipt_marks_fallback_and_cas_success(tmp_path):
    case = _historical_phase_c_case(tmp_path)
    try:
        local_board = case.db.get(Board, case.legacy_board_id)
        local_board.pinterest_board_id = None
        case.db.commit()

        original_external_link = case.attempt.provider_external_link
        original_attempt_count = case.db.query(PublicationAttempt).filter_by(
            publication_id=case.publication.id
        ).count()

        class HistoricalFallbackGateway(ExactGateway):
            async def post(self, operation_id):
                snapshot = await super().post(operation_id)
                return BufferPostSnapshot(
                    buffer_post_id=snapshot.buffer_post_id,
                    status=snapshot.status,
                    channel_id=snapshot.channel_id,
                    created_at=snapshot.created_at,
                    due_at=snapshot.due_at,
                    sent_at=snapshot.sent_at,
                    external_link=None,
                    channel_service=snapshot.channel_service,
                    text=snapshot.text,
                    pinterest_board_service_id=snapshot.pinterest_board_service_id,
                    pinterest_title=snapshot.pinterest_title,
                    pinterest_url=snapshot.pinterest_url,
                    image_url=snapshot.image_url,
                    image_alt_text=snapshot.image_alt_text,
                )

        gateway = HistoricalFallbackGateway(case.publication)
        result = _run(case, gateway)
        case.db.refresh(case.attempt)

        assert gateway.calls == [PILOT4_OPERATION]
        assert result.status == PublicationStatus.PUBLISHED
        assert result.pinterest_pin_id == PILOT4_PIN
        assert case.attempt.status == "SUCCEEDED"
        assert case.attempt.provider_pin_id == PILOT4_PIN
        assert case.attempt.provider_external_link == original_external_link
        assert case.db.query(PublicationAttempt).filter_by(
            publication_id=case.publication.id
        ).count() == original_attempt_count

        receipt = attest_buffer_reconciliation_receipt(case.db, case.publication.id)
        assert receipt["phase"] == "CAS_SUCCEEDED"
        assert receipt["provider_read_started"] is True
        assert receipt["provider_read_completed"] is True
        assert receipt["provider_operation_status"] == "sent"
        assert receipt["fallback_selected"] is True
        assert receipt["code"] is None
    finally:
        case.db.close()
        case.engine.dispose()


def test_receipt_attestation_is_provider_free_read_only_and_history_is_bounded(
    tmp_path,
    monkeypatch,
):
    case = _historical_phase_c_case(tmp_path)
    try:
        local_board = case.db.get(Board, case.legacy_board_id)
        local_board.pinterest_board_id = "different-external-board"
        case.db.commit()

        class ForbiddenGateway:
            def __init__(self, *args, **kwargs):
                raise AssertionError("receipt attestation must not construct BufferGateway")

        monkeypatch.setattr(reconciliation_service, "BufferGateway", ForbiddenGateway)

        for _ in range(10):
            with pytest.raises(BufferReconciliationError):
                asyncio.run(
                    reconcile_buffer(
                        case.db,
                        case.publication.id,
                        actor="operator",
                        settings=SETTINGS,
                        gateway=ExactGateway(case.publication),
                    )
                )

        case.db.expire_all()
        attempt = case.db.get(PublicationAttempt, case.attempt.id)
        history = (attempt.safe_response_metadata or {}).get(RECONCILIATION_RECEIPTS_KEY)
        assert isinstance(history, list)
        assert len(history) == 8

        before = dict(attempt.safe_response_metadata or {})
        receipt = attest_buffer_reconciliation_receipt(case.db, case.publication.id)
        assert receipt["provider_free"] is True
        assert receipt["read_only"] is True
        assert receipt["invocation_count"] == 10
        assert receipt["sequence"] == 10
        assert receipt["phase"] == "PRE_PROVIDER_REJECTED"
        assert not case.db.new
        assert not case.db.dirty
        assert not case.db.deleted
        case.db.expire_all()
        after = dict(case.db.get(PublicationAttempt, case.attempt.id).safe_response_metadata or {})
        assert after == before
    finally:
        case.db.close()
        case.engine.dispose()
