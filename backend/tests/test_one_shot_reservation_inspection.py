"""Pure/isolated tests of fail-closed reservation inspection."""
from types import SimpleNamespace

import pytest

from app.state_transfer.one_shot_operation_reservation import OneShotReservationRefusal
from app.state_transfer.one_shot_reservation_inspection import inspect_reservation


KEY = "task61-76-test-inspect"


class FakeDB:
    def __init__(self, record=None, dialect="postgresql"):
        self.record = record
        self.dialect = dialect
        self.reads = 0

    def get_bind(self):
        return SimpleNamespace(dialect=SimpleNamespace(name=self.dialect))

    def execute(self, statement):
        self.reads += 1
        assert statement.is_select
        return SimpleNamespace(mappings=lambda: SimpleNamespace(
            one_or_none=lambda: self.record
        ))


def test_absent_reservation_is_not_implicitly_created():
    db = FakeDB()
    assert inspect_reservation(db, operation_key=KEY) is None
    assert db.reads == 1


@pytest.mark.parametrize("state", ["RESERVED", "PREPARING", "READY", "FAILED", "UNCERTAIN"])
def test_existing_reservation_is_immutable_evidence(state):
    db = FakeDB(dict(operation_key=KEY, preflight_fingerprint="a" * 64,
                     batch_id="00000000-0000-4000-8000-000000000000", state=state))
    snapshot = inspect_reservation(db, operation_key=KEY)
    assert snapshot.state == state
    assert snapshot.operation_key == KEY
    assert db.reads == 1
    with pytest.raises(AttributeError):
        snapshot.state = "READY"


@pytest.mark.parametrize(("key", "dialect", "code"), [
    ("unsafe", "postgresql", "ONE_SHOT_OPERATION_KEY_INVALID"),
    (None, "postgresql", "ONE_SHOT_OPERATION_KEY_INVALID"),
    (KEY, "sqlite", "ONE_SHOT_POSTGRES_REQUIRED"),
])
def test_invalid_context_refuses_without_query(key, dialect, code):
    db = FakeDB(dialect=dialect)
    with pytest.raises(OneShotReservationRefusal) as e:
        inspect_reservation(db, operation_key=key)
    assert e.value.code == code
    assert db.reads == 0


def test_unknown_ledger_state_refuses():
    db = FakeDB(dict(operation_key=KEY, preflight_fingerprint="a" * 64,
                     batch_id="batch", state="UNKNOWN"))
    with pytest.raises(OneShotReservationRefusal) as e:
        inspect_reservation(db, operation_key=KEY)
    assert e.value.code == "ONE_SHOT_STATE_INVALID"
