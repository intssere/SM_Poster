"""Offline diagnostics tests: never stringify exceptions or consult a database."""
import json
from types import SimpleNamespace

import pytest

from app.state_transfer.capture_diagnostics import CaptureStage, safe_diagnostic


POISON = "postgresql://private-user:private-password@private-host/private-db ciphertext https://private.invalid"


class PoisonError(Exception):
    sqlstate = "42601"
    diag = SimpleNamespace(statement_position="123", message_primary=POISON,
                           message_detail=POISON, message_hint=POISON, context=POISON,
                           schema_name=POISON, table_name=POISON, column_name=POISON)

    def __str__(self):
        raise AssertionError("Exception text must never be inspected")

    def __repr__(self):
        raise AssertionError("Exception repr must never be inspected")


def test_allowlist_never_reads_or_returns_messages():
    diagnostic = safe_diagnostic(PoisonError(POISON), CaptureStage.EXECUTE_SINGLE_STATEMENT)
    assert diagnostic == {"stage": "EXECUTE_SINGLE_STATEMENT",
                          "exception_class": "PoisonError", "sqlstate": "42601",
                          "statement_position": 123}
    assert POISON not in json.dumps(diagnostic)


@pytest.mark.parametrize("value", [
    None, "", "0", "-1", "+1", " 1", "1 ", "1.0", "１２３", "123\n", "2147483648",
    0, -1, True, 1.0, POISON,
])
def test_invalid_statement_positions_are_omitted(value):
    error = PoisonError(POISON)
    error.diag = SimpleNamespace(statement_position=value)
    assert "statement_position" not in safe_diagnostic(error, CaptureStage.FETCH_ONE_ROW)


@pytest.mark.parametrize("value", ["1", 1, "2147483647", 2147483647])
def test_valid_statement_positions_are_numeric(value):
    error = PoisonError(POISON)
    error.diag = SimpleNamespace(statement_position=value)
    actual = safe_diagnostic(error, CaptureStage.EXECUTE_SINGLE_STATEMENT)
    assert type(actual["statement_position"]) is int
    assert actual["statement_position"] == int(value)


@pytest.mark.parametrize("value", [None, "", "22012\n", "42601 detail", "abcde", POISON, 22012])
def test_invalid_sqlstate_is_omitted(value):
    error = PoisonError(POISON)
    error.sqlstate = value
    assert "sqlstate" not in safe_diagnostic(error, CaptureStage.CONNECT)


def test_broken_diagnostic_properties_and_unsafe_class_name():
    class BrokenError(Exception):
        @property
        def sqlstate(self):
            raise ValueError(POISON)

        @property
        def diag(self):
            raise ValueError(POISON)

    assert safe_diagnostic(BrokenError(POISON), CaptureStage.CONNECT) == {
        "stage": "CONNECT", "exception_class": "BrokenError"}
    malicious = type("Exception\n" + POISON, (Exception,), {})
    assert safe_diagnostic(malicious(POISON), CaptureStage.CONNECT) == {
        "stage": "CONNECT", "exception_class": "Exception"}


def test_stage_enum_is_precise_and_closed():
    assert {stage.value for stage in CaptureStage} == {
        "CONNECT", "SESSION_SETUP", "EXECUTE_SINGLE_STATEMENT", "FETCH_ONE_ROW",
        "VALIDATE_SINGLE_ROW", "WRITE_CAPSULE_FILE", "HASH_CAPSULE", "OFFLINE_WRAP",
        "ROLLBACK/CLOSE",
    }