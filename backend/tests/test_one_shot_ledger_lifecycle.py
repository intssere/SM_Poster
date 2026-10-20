"""Task 61.76I: lifecycle ordering, duplicate immunity and terminal uncertainty."""
from unittest.mock import Mock

import pytest

from app.state_transfer.one_shot_ledger_lifecycle import (
    OneShotLifecycleRefusal, coordinate_ledger_lifecycle,
)
from app.state_transfer.one_shot_preparation_intent import OneShotPreparationIntent


def intent():
    return OneShotPreparationIntent(
        operation_key="task61-76-test-ledger",
        batch_id="00000000-0000-4000-8000-000000000000",
        preflight_fingerprint="a" * 64,
        plan_id="00000000-0000-4000-8000-000000000001",
        plan_fingerprint="b" * 64,
        candidate_item_ids=tuple(str(i) for i in range(5)),
        current_date="2026-10-21",
    )


def invoke(*, fail=None, certified=True, bound=None, preflight=None):
    value = intent() if bound is None else bound
    events = []
    def reserve(db, item):
        events.append("reserve")
        if fail == "reserve":
            raise RuntimeError("duplicate or uncertain reservation")
    def transition(db, *, operation_key, expected_state, next_state):
        events.append(expected_state + "->" + next_state)
        if fail == "transition" and next_state == "PREPARING":
            raise RuntimeError("commit acknowledgment lost")
    def create(db, *, settings, batch_id):
        events.append("create")
        if fail == "create":
            raise RuntimeError("batch uncertain")
        return batch_id
    def prepare(db, batch_id, plan_id, *, settings, expected_preflight):
        events.append("prepare")
        if fail == "prepare":
            raise RuntimeError("internal records uncertain")
        return batch_id
    def certify(item):
        events.append("certify")
        if fail == "certify":
            raise RuntimeError("read-only cert unavailable")
        return certified
    def run():
        return coordinate_ledger_lifecycle(
            db=object(), intent=value, settings=object(),
            expected_preflight={"preflight_fingerprint": "a" * 64}
            if preflight is None else preflight,
            reserve=reserve, create=create, prepare=prepare,
            certify=certify, transition=transition,
        )
    return run, events


def test_success_sequence_terminal_ready():
    run, events = invoke()
    result = run()
    assert result.state == "READY"
    assert events == [
        "reserve", "RESERVED->PREPARING", "create", "prepare",
        "certify", "PREPARING->READY",
    ]


@pytest.mark.parametrize("fail,expected", [
    ("reserve", ["reserve"]),
    ("transition", ["reserve", "RESERVED->PREPARING"]),
    ("create", ["reserve", "RESERVED->PREPARING", "create", "PREPARING->UNCERTAIN"]),
    ("prepare", ["reserve", "RESERVED->PREPARING", "create", "prepare", "PREPARING->UNCERTAIN"]),
    ("certify", ["reserve", "RESERVED->PREPARING", "create", "prepare", "certify", "PREPARING->UNCERTAIN"]),
])
def test_failure_stops_without_retries_or_duplicate_mutation(fail, expected):
    run, events = invoke(fail=fail)
    with pytest.raises(RuntimeError):
        run()
    assert events == expected


def test_negative_certification_is_not_success():
    run, events = invoke(certified=False)
    with pytest.raises(RuntimeError, match="ONE_SHOT_INDEPENDENT_CERTIFICATION_REQUIRED"):
        run()
    assert events[-1] == "PREPARING->UNCERTAIN"


def test_invalid_intent_and_preflight_refuse_before_reserving():
    run, events = invoke(bound=None, preflight={"preflight_fingerprint": "b" * 64})
    with pytest.raises(OneShotLifecycleRefusal, match="ONE_SHOT_PREFLIGHT_MISMATCH"):
        run()
    assert events == []
