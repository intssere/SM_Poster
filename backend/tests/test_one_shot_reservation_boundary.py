"""Task 61.76G reservation barrier is non-executing and fail-closed."""
from dataclasses import fields
from unittest.mock import Mock

import pytest

from app.state_transfer import one_shot_reservation_boundary as boundary
from app.state_transfer.one_shot_preparation_intent import OneShotPreparationIntent
from app.state_transfer.one_shot_operation_reservation import OneShotReservationRefusal


def _intent():
    return OneShotPreparationIntent(
        operation_key="task61-76-test-operation",
        batch_id="00000000-0000-4000-8000-000000000000",
        preflight_fingerprint="a" * 64,
        plan_id="00000000-0000-4000-8000-000000000001",
        plan_fingerprint="b" * 64,
        candidate_item_ids=tuple(str(i) for i in range(5)),
        current_date="2026-10-15",
    )


def test_reservation_barrier_calls_existing_committing_primitive_exactly_once(monkeypatch):
    reserve = Mock()
    monkeypatch.setattr(boundary, "reserve_once", reserve)
    db = object()
    assert boundary.reserve_preparation_intent(db, _intent()) is None
    reserve.assert_called_once_with(
        db,
        operation_key="task61-76-test-operation",
        preflight_fingerprint="a" * 64,
        batch_id="00000000-0000-4000-8000-000000000000",
    )


def test_invalid_intent_refuses_before_reservation(monkeypatch):
    reserve = Mock()
    monkeypatch.setattr(boundary, "reserve_once", reserve)
    for invalid in (None, {}, object()):
        with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_VALIDATED_INTENT_REQUIRED"):
            boundary.reserve_preparation_intent(object(), invalid)
    reserve.assert_not_called()


def test_uncertain_reservation_propagates_without_retry(monkeypatch):
    reserve = Mock(side_effect=OneShotReservationRefusal("ONE_SHOT_RESERVATION_UNCERTAIN"))
    monkeypatch.setattr(boundary, "reserve_once", reserve)
    with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_RESERVATION_UNCERTAIN"):
        boundary.reserve_preparation_intent(object(), _intent())
    assert reserve.call_count == 1
