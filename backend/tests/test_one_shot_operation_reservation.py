"""Pure contract tests. PostgreSQL integration/migration coverage is a later gate."""
from types import SimpleNamespace
import pytest

from app.state_transfer.one_shot_operation_reservation import (
    OneShotReservationRefusal, reserve_once, mark_outcome, reservations,
)

KEY = "task61-76-first-five"
FP = "a" * 64
BATCH = "12345678-1234-1234-1234-123456789abc"


class FakeSession:
    def __init__(self, *, rowcount=1, fail=False, dialect="postgresql"):
        self.rowcount = rowcount
        self.fail = fail
        self.dialect = dialect
        self.commits = 0
        self.rollbacks = 0
        self.calls = []

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def execute(self, statement):
        self.calls.append(statement)
        if self.fail:
            raise RuntimeError("simulated connection lost after unknown write")
        return SimpleNamespace(rowcount=self.rowcount)

    def commit(self):
        self.commits += 1

    def rollback(self):
        self.rollbacks += 1


def test_reservation_commits_once_and_targets_unique_key():
    db = FakeSession()
    reserve_once(db, operation_key=KEY, preflight_fingerprint=FP, batch_id=BATCH)
    assert db.commits == 1 and db.rollbacks == 0 and len(db.calls) == 1
    statement = str(db.calls[0].compile(dialect=__import__(
        "sqlalchemy.dialects.postgresql", fromlist=["dialect"]).dialect()))
    assert "ON CONFLICT (operation_key) DO NOTHING" in statement


def test_duplicate_is_refused_without_new_batch_attempt():
    db = FakeSession(rowcount=0)
    with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_OPERATION_ALREADY_CONSUMED"):
        reserve_once(db, operation_key=KEY, preflight_fingerprint=FP, batch_id=BATCH)
    assert db.commits == 1


def test_connection_uncertainty_is_never_treated_as_retry():
    db = FakeSession(fail=True)
    with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_RESERVATION_UNCERTAIN"):
        reserve_once(db, operation_key=KEY, preflight_fingerprint=FP, batch_id=BATCH)
    assert db.rollbacks == 1


@pytest.mark.parametrize("kwargs", [
    dict(operation_key="other-task", preflight_fingerprint=FP, batch_id=BATCH),
    dict(operation_key=KEY, preflight_fingerprint="invalid", batch_id=BATCH),
    dict(operation_key=KEY, preflight_fingerprint=FP, batch_id=""),
])
def test_refuses_invalid_identity_before_sql(kwargs):
    db = FakeSession()
    with pytest.raises(OneShotReservationRefusal):
        reserve_once(db, **kwargs)
    assert not db.calls


def test_postgresql_only():
    db = FakeSession(dialect="sqlite")
    with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_POSTGRES_REQUIRED"):
        reserve_once(db, operation_key=KEY, preflight_fingerprint=FP, batch_id=BATCH)


def test_transition_compare_and_set():
    db = FakeSession()
    mark_outcome(db, operation_key=KEY, expected_state="RESERVED", next_state="PREPARING")
    assert db.commits == 1 and len(db.calls) == 1


@pytest.mark.parametrize("start,end", [
    ("READY", "PREPARING"), ("FAILED", "RESERVED"),
    ("UNCERTAIN", "PREPARING"), ("PREPARING", "RESERVED"),
])
def test_terminal_states_are_not_reopened(start, end):
    db = FakeSession()
    with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_TRANSITION_FORBIDDEN"):
        mark_outcome(db, operation_key=KEY, expected_state=start, next_state=end)
    assert not db.calls


def test_cas_mismatch_fail_closed():
    db = FakeSession(rowcount=0)
    with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_STATE_MISMATCH"):
        mark_outcome(db, operation_key=KEY, expected_state="RESERVED", next_state="PREPARING")
    assert db.rollbacks == 1 and db.commits == 0


def test_outcome_uncertainty_fails_closed():
    db = FakeSession(fail=True)
    with pytest.raises(OneShotReservationRefusal, match="ONE_SHOT_TRANSITION_UNCERTAIN"):
        mark_outcome(db, operation_key=KEY, expected_state="PREPARING", next_state="READY")
    assert db.rollbacks == 1
