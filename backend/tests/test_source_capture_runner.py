"""Faked direct-client capture; no live secrets, providers, or database connections."""
import hashlib
import importlib.util
import json
import logging
from pathlib import Path
import stat
from types import SimpleNamespace

import pytest

from app.state_transfer import capture_runner as runner
from app.state_transfer.bridge_json import pg_json
from app.state_transfer.select_bridge import source_sql
from tests.test_source_capture_diagnostics import POISON, PoisonError
from tests.test_migration_closed_state_transfer import (
    pytestmark as disposable_postgres_required,
    source,
)


class FakeCursor:
    description = [SimpleNamespace(name="capsule")]

    def __init__(self, rows, error_stage=None, error=None):
        self.rows = iter(rows)
        self.executed = []
        self.error_stage = error_stage
        self.error = error or PoisonError(POISON)
        self.closed = False

    def execute(self, sql, **kwargs):
        self.executed.append((sql, kwargs))
        if ((len(self.executed) == 1 and self.error_stage == "setup")
                or (len(self.executed) == 2 and self.error_stage == "execute")):
            raise self.error

    def fetchone(self):
        if self.error_stage == "fetch":
            raise self.error
        return next(self.rows, None)

    def close(self):
        self.closed = True
        if self.error_stage == "cursor_close":
            raise self.error


class FakeConnection:
    def __init__(self, cursor):
        self.fake_cursor = cursor
        self.rolled_back = False
        self.closed = False

    def cursor(self):
        return self.fake_cursor

    def rollback(self):
        self.rolled_back = True
        if self.fake_cursor.error_stage == "rollback":
            raise self.fake_cursor.error

    def close(self):
        self.closed = True
        if self.fake_cursor.error_stage == "close":
            raise self.fake_cursor.error


@pytest.fixture
def capsule():
    payload = {"private": POISON}
    return {"payload": payload,
            "capsule_sha256": hashlib.sha256(pg_json(payload).encode()).hexdigest()}


def run_capture(tmp_path, cursor, monkeypatch, *, wrapper_error=False, write_error=False):
    connection = FakeConnection(cursor)
    calls = []
    def connect(dsn, **kwargs):
        calls.append((dsn, kwargs))
        return connection
    def wrapper(value, expected_capsule_sha256):
        assert expected_capsule_sha256 == value["capsule_sha256"]
        assert connection.closed and connection.rolled_back
        if wrapper_error:
            raise PoisonError(POISON)
        return {"manifest": {}, "rows": {"private": POISON}}
    monkeypatch.setattr(runner, "wrap_source_result", wrapper)
    monkeypatch.setattr(runner, "safe_plan", lambda bundle: {"manifest_sha256": "a" * 64})
    if write_error:
        def bad_write(*args):
            raise OSError(POISON)
        monkeypatch.setattr(runner, "_write_capsule", bad_write)
    result = runner.capture_source_result(
        load_dsn=lambda: POISON, capsule_file=tmp_path / "capsule.json",
        bundle_file=tmp_path / "bundle.json", connect=connect)
    encoded = json.dumps(result)
    assert POISON not in encoded
    assert "private-user" not in encoded and "ciphertext" not in encoded
    assert len(calls) == 1 and calls[0][1]["autocommit"] is True
    assert "default_transaction_read_only=on" in calls[0][1]["options"]
    assert connection.closed and connection.rolled_back and cursor.closed
    assert len(cursor.executed) <= 2
    if len(cursor.executed) == 2:
        assert cursor.executed[1] == (source_sql(), {"prepare": False})
    return result


@pytest.mark.parametrize("sqlstate", ["42601", "22012"])
def test_postgres_execution_and_fail_closed_division_errors(tmp_path, monkeypatch, capsule, sqlstate):
    error = PoisonError(POISON)
    error.sqlstate = sqlstate
    cursor = FakeCursor([(capsule,)], "execute", error)
    result = run_capture(tmp_path, cursor, monkeypatch)
    assert result == {"success": False, "diagnostic": {
        "stage": "EXECUTE_SINGLE_STATEMENT", "exception_class": "PoisonError",
        "sqlstate": sqlstate, "statement_position": 123}}
    assert not (tmp_path / "capsule.json").exists()


@pytest.mark.parametrize("exception_class,sqlstate", [
    ("SyntaxError", "42601"), ("DivisionByZero", "22012"),
])
def test_real_postgres_exception_classes_are_preserved(tmp_path, monkeypatch, exception_class, sqlstate):
    import psycopg.errors
    error = getattr(psycopg.errors, exception_class)(POISON)
    result = run_capture(tmp_path, FakeCursor([], "execute", error), monkeypatch)
    assert result["diagnostic"] == {
        "stage": "EXECUTE_SINGLE_STATEMENT", "exception_class": exception_class,
        "sqlstate": sqlstate,
    }


@pytest.mark.parametrize("where,stage", [
    ("setup", "SESSION_SETUP"), ("fetch", "FETCH_ONE_ROW"),
    ("rollback", "ROLLBACK/CLOSE"), ("close", "ROLLBACK/CLOSE"),
    ("cursor_close", "ROLLBACK/CLOSE"),
])
def test_precise_failure_stages(tmp_path, monkeypatch, capsule, where, stage):
    result = run_capture(tmp_path, FakeCursor([(capsule,)], where), monkeypatch)
    assert result["diagnostic"]["stage"] == stage
    assert not (tmp_path / "bundle.json").exists()


@pytest.mark.parametrize("rows", [
    [], [(None,)], [("malformed " + POISON,)], [({},)],
    [("{}",)], [(b"\xff",)], [(123,)], [(None, None)],
])
def test_malformed_missing_rows_are_safe(tmp_path, monkeypatch, rows):
    result = run_capture(tmp_path, FakeCursor(rows), monkeypatch)
    assert result["success"] is False
    assert result["diagnostic"]["stage"] == "VALIDATE_SINGLE_ROW"


def test_multiple_rows_are_refused_without_retry(tmp_path, monkeypatch, capsule):
    result = run_capture(tmp_path, FakeCursor([(capsule,), (capsule,)]), monkeypatch)
    assert result["diagnostic"]["stage"] == "VALIDATE_SINGLE_ROW"
    assert not (tmp_path / "capsule.json").exists()


def test_invalid_column_shape_is_refused(tmp_path, monkeypatch, capsule):
    cursor = FakeCursor([(capsule,)])
    cursor.description = [SimpleNamespace(name=POISON)]
    result = run_capture(tmp_path, cursor, monkeypatch)
    assert result["diagnostic"]["stage"] == "VALIDATE_SINGLE_ROW"


def test_file_write_failure(tmp_path, monkeypatch, capsule):
    result = run_capture(tmp_path, FakeCursor([(capsule,)]), monkeypatch, write_error=True)
    assert result["diagnostic"] == {"stage": "WRITE_CAPSULE_FILE", "exception_class": "OSError"}


def test_hash_failure_is_safe(tmp_path, monkeypatch, capsule):
    capsule["capsule_sha256"] = POISON
    result = run_capture(tmp_path, FakeCursor([(capsule,)]), monkeypatch)
    assert result["diagnostic"] == {"stage": "HASH_CAPSULE", "exception_class": "ValueError"}


def test_wrapper_failure_is_safe(tmp_path, monkeypatch, capsule):
    result = run_capture(tmp_path, FakeCursor([(capsule,)]), monkeypatch, wrapper_error=True)
    assert result["diagnostic"]["stage"] == "OFFLINE_WRAP"
    assert stat.S_IMODE((tmp_path / "capsule.json").stat().st_mode) == 0o600
    assert not (tmp_path / "bundle.json").exists()


def test_private_success_and_no_stdout(tmp_path, monkeypatch, capsule, capsys):
    before = logging.root.manager.disable
    result = run_capture(tmp_path, FakeCursor([(capsule,)]), monkeypatch)
    assert result["success"] is True
    assert result["source_capsule_sha256"] == capsule["capsule_sha256"]
    assert logging.root.manager.disable == before
    assert capsys.readouterr() == ("", "")
    for name in ("capsule.json", "bundle.json"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600


def test_cleanup_errors_do_not_mask_execution_error(tmp_path, monkeypatch):
    cursor = FakeCursor([], "execute")
    connection = FakeConnection(cursor)
    def broken_rollback():
        raise OSError(POISON)
    connection.rollback = broken_rollback
    result = runner.capture_source_result(
        load_dsn=lambda: POISON, capsule_file=tmp_path / "capsule.json",
        bundle_file=tmp_path / "bundle.json", connect=lambda *a, **k: connection)
    assert result["diagnostic"]["stage"] == "EXECUTE_SINGLE_STATEMENT"
    assert result["cleanup_diagnostics"] == [
        {"stage": "ROLLBACK/CLOSE", "exception_class": "OSError"}]
    assert connection.closed and POISON not in json.dumps(result)


def test_partial_file_removed_and_existing_file_not_overwritten(tmp_path, monkeypatch):
    path = tmp_path / "capsule.json"
    def fail_fsync(*args):
        raise OSError(POISON)
    monkeypatch.setattr(runner.os, "fsync", fail_fsync)
    with pytest.raises(OSError):
        runner._write_capsule(path, POISON.encode())
    assert not path.exists()
    path.write_bytes(b"existing")
    with pytest.raises(FileExistsError):
        runner._write_capsule(path, b"replacement")
    assert path.read_bytes() == b"existing"


def load_cli():
    path = Path(__file__).resolve().parents[2] / "scripts" / "transfer_production_state.py"
    spec = importlib.util.spec_from_file_location("capture_test_cli", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_cli_connect_failure_is_sanitized(tmp_path, monkeypatch, capsys):
    import psycopg
    def fail_connect(*args, **kwargs):
        raise PoisonError(POISON)
    monkeypatch.setattr(psycopg, "connect", fail_connect)
    dsn_file = tmp_path / "synthetic-dsn"
    dsn_file.write_text(POISON)
    code = load_cli().main(["capture-source-result", "--dsn-file", str(dsn_file),
                           "--capsule-file", str(tmp_path / "capsule"),
                           "--bundle", str(tmp_path / "bundle")])
    captured = capsys.readouterr()
    assert code == 2 and captured.err == "" and POISON not in captured.out
    assert json.loads(captured.out) == {"success": False, "diagnostic": {
        "stage": "CONNECT", "exception_class": "PoisonError", "sqlstate": "42601",
        "statement_position": 123}}


def test_cli_refuses_implicit_or_ambiguous_connection(tmp_path, capsys):
    cli = load_cli()
    for options in ([], ["--dsn-file", "synthetic", "--dsn-env", "SYNTHETIC"]):
        assert cli.main(["capture-source-result", "--capsule-file", str(tmp_path / "capsule"),
                         "--bundle", str(tmp_path / "bundle"), *options]) == 2
        output = capsys.readouterr()
        assert output.err == ""
        assert json.loads(output.out)["diagnostic"] == {
            "stage": "CONNECT", "exception_class": "ValueError"}


def test_cli_missing_explicit_secret_name_never_connects(tmp_path, monkeypatch, capsys):
    import psycopg
    def forbidden_connect(*args, **kwargs):
        pytest.fail("Missing operator input must not fall back to any database")
    monkeypatch.setattr(psycopg, "connect", forbidden_connect)
    assert load_cli().main([
        "capture-source-result", "--dsn-env", "MISSING_SYNTHETIC_CAPTURE_SECRET",
        "--capsule-file", str(tmp_path / "capsule"), "--bundle", str(tmp_path / "bundle"),
    ]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert json.loads(output.out) == {"success": False, "diagnostic": {
        "stage": "CONNECT", "exception_class": "KeyError"}}


@disposable_postgres_required
def test_real_disposable_direct_client_capture(source, tmp_path):
    """Only the credential-fenced test runner's local disposable database."""
    from app.state_transfer.transfer import verify_bundle
    result = runner.capture_source_result(
        load_dsn=lambda: source.url.set(drivername="postgresql").render_as_string(
            hide_password=False),
        capsule_file=tmp_path / "capsule.json", bundle_file=tmp_path / "bundle.json")
    assert result["success"] is True
    bundle = json.loads((tmp_path / "bundle.json").read_text())
    verify_bundle(bundle, result["manifest_sha256"])
    assert bundle["manifest"]["snapshot"]["read_only"] == "on"
    assert bundle["manifest"]["snapshot"]["isolation"] == "repeatable read"
    assert sum(t["source_count"] for t in result["tables"].values()) == 12459
    assert sum(t["exported_count"] for t in result["tables"].values()) == 12454
    for name in ("capsule.json", "bundle.json"):
        assert stat.S_IMODE((tmp_path / name).stat().st_mode) == 0o600