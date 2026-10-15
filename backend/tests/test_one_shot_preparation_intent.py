"""Task 61.76E: pure admission intent cannot execute provider or DB writes."""
from __future__ import annotations

from dataclasses import FrozenInstanceError
from uuid import uuid4

import pytest

from app.state_transfer import one_shot_preparation_intent as intent


TODAY = "2026-10-12"
OP = "task61-76-five-pin-prepare-october"
FINGERPRINT = "a" * 64
PLAN_FINGERPRINT = "b" * 64


def _report():
    return {"database_revision": "0035"}


def _receipt():
    return {
        "preflight_fingerprint": FINGERPRINT,
        "plan_fingerprint": PLAN_FINGERPRINT,
        "plan_id": str(uuid4()),
        "current_date": TODAY,
        "candidates": [{"item_id": str(uuid4())} for _ in range(5)],
    }


def test_valid_intent_is_immutable_and_preserves_order(monkeypatch):
    receipt = _receipt()
    seen = []
    def validate(report, *, today):
        seen.append((report, today))
        return receipt
    monkeypatch.setattr(intent, "validate_preflight_for_one_shot", validate)
    batch_id = str(uuid4())
    prepared = intent.build_intent(
        report=_report(), operation_key=OP, batch_id=batch_id, today=TODAY,
    )
    assert prepared.batch_id == batch_id
    assert prepared.preflight_fingerprint == FINGERPRINT
    assert prepared.plan_fingerprint == PLAN_FINGERPRINT
    assert prepared.candidate_item_ids == tuple(x["item_id"] for x in receipt["candidates"])
    assert seen == [(_report(), TODAY)]
    with pytest.raises(FrozenInstanceError):
        prepared.batch_id = str(uuid4())


@pytest.mark.parametrize(("field", "value", "code"), [
    ("report", {"database_revision": "0034"}, "ONE_SHOT_SCHEMA_0035_REQUIRED"),
    ("report", {}, "ONE_SHOT_SCHEMA_0035_REQUIRED"),
    ("operation_key", "task61-76-", "ONE_SHOT_OPERATION_KEY_INVALID"),
    ("operation_key", "another-operation", "ONE_SHOT_OPERATION_KEY_INVALID"),
    ("operation_key", None, "ONE_SHOT_OPERATION_KEY_INVALID"),
    ("batch_id", "not-a-uuid", "ONE_SHOT_BATCH_ID_INVALID"),
    ("batch_id", str(uuid4()).upper(), "ONE_SHOT_BATCH_ID_INVALID"),
    ("today", "2026-02-30", "ONE_SHOT_DATE_INVALID"),
    ("today", "2026-10-12T00:00", "ONE_SHOT_DATE_INVALID"),
])
def test_invalid_binding_refuses_before_receipt_validation(monkeypatch, field, value, code):
    def should_not_run(*args, **kwargs):
        raise AssertionError("must refuse before receipt validation")
    monkeypatch.setattr(intent, "validate_preflight_for_one_shot", should_not_run)
    args = dict(report=_report(), operation_key=OP, batch_id=str(uuid4()), today=TODAY)
    args[field] = value
    with pytest.raises(intent.OneShotIntentRefusal) as exc:
        intent.build_intent(**args)
    assert exc.value.code == code


def test_existing_preflight_refusal_propagates_without_partial_intent(monkeypatch):
    from app.state_transfer.one_shot_bounded_admission import PreflightAdmissionRefusal
    def reject(*args, **kwargs):
        raise PreflightAdmissionRefusal("ONE_SHOT_PREFLIGHT_NOT_ADMITTED")
    monkeypatch.setattr(intent, "validate_preflight_for_one_shot", reject)
    with pytest.raises(PreflightAdmissionRefusal):
        intent.build_intent(report=_report(), operation_key=OP,
                            batch_id=str(uuid4()), today=TODAY)
