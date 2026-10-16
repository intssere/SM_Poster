"""Task 61.76H coordinator ordering and no-retry regressions."""
from unittest.mock import Mock

import pytest

from app.state_transfer.one_shot_preparation_coordinator import (
    OneShotCoordinatorRefusal, coordinate_once,
)
from app.state_transfer.one_shot_preparation_intent import OneShotPreparationIntent


def intent():
    return OneShotPreparationIntent(
        operation_key="task61-76-test-coordinator",
        batch_id="00000000-0000-4000-8000-000000000000",
        preflight_fingerprint="a" * 64,
        plan_id="00000000-0000-4000-8000-000000000001",
        plan_fingerprint="b" * 64,
        candidate_item_ids=tuple(str(i) for i in range(5)),
        current_date="2026-10-16",
    )


def make_calls(*, fail_at=None, certified=True):
    events = []
    value = intent()
    def reserve(db, bound):
        events.append("reserve")
        assert bound is value or bound == value
        if fail_at == "reserve":
            raise RuntimeError("ambiguous committed reservation")
    def create(db, *, settings, batch_id):
        events.append("create")
        if fail_at == "create":
            raise RuntimeError("ambiguous committed batch")
        return batch_id
    def prepare(db, batch_id, plan_id, *, settings, expected_preflight):
        events.append("prepare")
        if fail_at == "prepare":
            raise RuntimeError("ambiguous committed internal artifacts")
        return batch_id
    def certify(bound):
        events.append("certify")
        if fail_at == "certify":
            raise RuntimeError("independent certification unavailable")
        return certified
    return value, events, (reserve, create, prepare, certify)


def execute(value, funcs, preflight=None):
    reserve, create, prepare, certify = funcs
    return coordinate_once(
        db=object(), intent=value, reserve=reserve, create=create,
        prepare=prepare, certify=certify, settings=object(),
        expected_preflight=preflight if preflight is not None else {
            "preflight_fingerprint": (
                value.preflight_fingerprint if isinstance(value, OneShotPreparationIntent)
                else "a" * 64
            ),
        },
    )


def test_happy_path_exact_call_order():
    value, events, funcs = make_calls()
    result = execute(value, funcs)
    assert (result.batch_id, result.certification) == (value.batch_id, "PASS")
    assert events == ["reserve", "create", "prepare", "certify"]


@pytest.mark.parametrize(("fail_at", "events_expected"), [
    ("reserve", ["reserve"]),
    ("create", ["reserve", "create"]),
    ("prepare", ["reserve", "create", "prepare"]),
    ("certify", ["reserve", "create", "prepare", "certify"]),
])
def test_failures_stop_without_retries(fail_at, events_expected):
    value, events, funcs = make_calls(fail_at=fail_at)
    with pytest.raises(RuntimeError):
        execute(value, funcs)
    assert events == events_expected


def test_uncertified_preparation_fails_closed():
    value, events, funcs = make_calls(certified=False)
    with pytest.raises(OneShotCoordinatorRefusal, match="ONE_SHOT_INDEPENDENT_CERTIFICATION_REQUIRED"):
        execute(value, funcs)
    assert events == ["reserve", "create", "prepare", "certify"]


def test_preflight_mismatch_refuses_before_any_callback():
    value, events, funcs = make_calls()
    with pytest.raises(OneShotCoordinatorRefusal, match="ONE_SHOT_PREFLIGHT_FINGERPRINT_MISMATCH"):
        execute(value, funcs, {"preflight_fingerprint": "b" * 64})
    assert events == []


def test_unvalidated_intent_refuses_before_callbacks():
    value, events, funcs = make_calls()
    with pytest.raises(OneShotCoordinatorRefusal, match="ONE_SHOT_VALIDATED_INTENT_REQUIRED"):
        execute(None, funcs)
    assert events == []
