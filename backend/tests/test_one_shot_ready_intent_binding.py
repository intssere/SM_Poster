"""Fail-closed one-shot READY identity binding regression tests."""
from copy import deepcopy

import pytest

from app.state_transfer.one_shot_preparation_intent import OneShotPreparationIntent
from app.state_transfer.one_shot_ready_intent_binding import (
    ReadyIntentBindingRefusal, require_ready_certificate,
)


def fixture():
    intent = OneShotPreparationIntent(
        operation_key="task61-76-test-certificate",
        batch_id="00000000-0000-4000-8000-000000000000",
        plan_id="00000000-0000-4000-8000-000000000001",
        plan_fingerprint="b" * 64,
        preflight_fingerprint="a" * 64,
        candidate_item_ids=tuple("item-" + str(i) for i in range(5)),
        current_date="2026-11-01",
    )
    report = dict(
        success=True, mode="READ_ONLY_FIVE_PIN_READY_BATCH_CERTIFICATION",
        ready_batch_certification="PASS", terminal_stage="COMPLETE",
        database_revision="0035", publishing_admission="NOT_GRANTED",
        routine_state="PAUSED", batch_state="READY",
        batch_id=intent.batch_id, plan_id=intent.plan_id,
        plan_fingerprint=intent.plan_fingerprint, target_count=5,
        candidate_count=5, attempts_reserved=0, admission_closed=False,
        ready_batch_fingerprint="c" * 64,
        entries=[{"slot": i, "item_id": item} for i, item in enumerate(intent.candidate_item_ids)],
    )
    for field in (
        "database_writes", "provider_calls", "provider_reads", "provider_writes",
        "buffer_calls", "pinterest_calls", "oauth_calls", "ai_calls",
        "automatic_retries", "scheduler_activations", "worker_activations",
        "autonomy_activations", "provider_attempts_reserved",
    ):
        report[field] = 0
    return intent, report


def test_exact_certificate_passes():
    intent, report = fixture()
    assert require_ready_certificate(intent, report) is True


@pytest.mark.parametrize(("field", "changed"), [
    ("success", False),
    ("ready_batch_certification", "NOT_GRANTED"),
    ("mode", "READ_ONLY_FIVE_PIN_BOUNDED_PREFLIGHT"),
    ("database_revision", "0034"),
    ("publishing_admission", "GRANTED"),
    ("routine_state", "ACTIVE"),
    ("batch_state", "PREPARING"),
    ("batch_id", "wrong-batch"),
    ("plan_id", "wrong-plan"),
    ("plan_fingerprint", "f" * 64),
    ("candidate_count", 4),
    ("target_count", 4),
    ("attempts_reserved", 1),
    ("admission_closed", True),
    ("ready_batch_fingerprint", "not-a-hash"),
    ("provider_calls", 1),
    ("provider_writes", None),
    ("database_writes", False),
    ("automatic_retries", 1),
])
def test_tampered_or_incomplete_certification_refused(field, changed):
    intent, report = fixture()
    report[field] = changed
    with pytest.raises(ReadyIntentBindingRefusal):
        require_ready_certificate(intent, report)


def test_candidate_swap_rejected_even_if_five_exist():
    intent, report = fixture()
    report["entries"][0]["item_id"], report["entries"][1]["item_id"] = (
        report["entries"][1]["item_id"], report["entries"][0]["item_id"]
    )
    with pytest.raises(ReadyIntentBindingRefusal, match="ONE_SHOT_READY_CANDIDATE_MISMATCH"):
        require_ready_certificate(intent, report)


def test_slot_swap_and_duplicate_rejected():
    intent, report = fixture()
    report["entries"][2]["slot"] = 0
    with pytest.raises(ReadyIntentBindingRefusal):
        require_ready_certificate(intent, report)
    intent, report = fixture()
    report["entries"][4]["item_id"] = report["entries"][3]["item_id"]
    with pytest.raises(ReadyIntentBindingRefusal):
        require_ready_certificate(intent, report)
