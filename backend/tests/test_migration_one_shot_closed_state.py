"""Disposable PostgreSQL only, behind credential/network/storage test fences."""
import importlib.util
import json
from pathlib import Path
import stat

import psycopg
import pytest
import sqlalchemy as sa

from app.state_transfer import capture_runner, one_shot_migration as once, transfer
from app.state_transfer.catalog import Refused
from app.state_transfer.select_bridge import source_sql
from tests.test_migration_closed_state_transfer import source
from tests.test_readiness_execution_admission_0032 import _isolated_database, pytestmark
from tests.test_source_capture_diagnostics import POISON, PoisonError


@pytest.fixture
def operator(monkeypatch, source, tmp_path):
    monkeypatch.setenv("SOURCE_FIXTURE", source.url.render_as_string(hide_password=False))
    invocations, statements, options = [], [], []
    expected_sql = source_sql()

    class ObservedCursor(psycopg.Cursor):
        def execute(self, query, *args, **kwargs):
            statements.append("capture" if query == expected_sql else "setup")
            return super().execute(query, *args, **kwargs)

    def capture(**kwargs):
        invocations.append(True)

        def connect(dsn, **config):
            options.append(config["options"])
            return psycopg.connect(dsn, cursor_factory=ObservedCursor, **config)

        return capture_runner.capture_source_result(connect=connect, **kwargs)

    monkeypatch.setattr(once, "capture_source_result", capture)

    def run(target, **kwargs):
        monkeypatch.setenv("TARGET_FIXTURE", target.url.render_as_string(hide_password=False))
        return once.run_migration(
            source_env="SOURCE_FIXTURE", target_env="TARGET_FIXTURE",
            ephemeral_parent=tmp_path, **{"execute": True, **kwargs},
        )

    return run, invocations, statements, options


def assert_clean(tmp_path):
    assert list(tmp_path.iterdir()) == []


def assert_empty(engine):
    with engine.connect() as c:
        assert transfer.count(c, "stores") == 0
        assert transfer.count(c, "products") == 0
        assert transfer.count(c, "pin_publications") == 0
        assert c.execute(sa.text(
            "SELECT id,state FROM public.routine_publishing_control"
        )).all() == [("default", "PAUSED")]


def test_success_single_exact_capture_certifies_and_cleans(operator, tmp_path, capsys):
    run, calls, statements, options = operator
    with _isolated_database("0034") as (target, _):
        result = run(target, statement_timeout_ms=480000, lock_timeout_ms=12000)
        assert result["success"] is True, result
        assert result["import_committed"] is True
        assert result["atomic_certification"] == result["durable_certification"] == "PASS"
        assert result["source_total"] == 12459 and result["imported_total"] == 12454
        assert result["tables"]["pinterest_oauth_states"] == {
            "source_count": 5, "imported_count": 0}
        assert result["read_only"] == "on" and result["isolation"] == "repeatable read"
        assert result["statement_count"] == 1 and calls == [True]
        assert statements == ["setup", "capture"]
        assert "statement_timeout=480000" in options[0] and "lock_timeout=12000" in options[0]
        assert "idle_in_transaction_session_timeout=240000" in options[0]
        with target.connect() as c:
            assert transfer.count(c, "products") == 2997
            assert transfer.count(c, "pinterest_oauth_states") == 0
    assert_clean(tmp_path)
    assert capsys.readouterr() == ("", "")


def test_repeated_invocation_refuses_unchanged_target(operator, tmp_path):
    run, calls, statements, _ = operator
    with _isolated_database("0034") as (target, _):
        first = run(target)
        assert first["success"] is True
        second = run(target)
        assert second["success"] is False
        assert second["diagnostic"]["stage"] == "TARGET_PREFLIGHT"
        assert second["import_committed"] is False
        with target.connect() as c:
            assert transfer.count(c, "products") == 2997
            assert transfer.count(c, "stores") == 1
    assert calls == [True, True] and statements.count("capture") == 2
    assert_clean(tmp_path)


def test_nonempty_target_refusal(operator, source, tmp_path):
    run, _, _, _ = operator
    with source.connect() as c:
        existing = dict(c.execute(sa.text("SELECT * FROM public.stores")).mappings().one())
    with _isolated_database("0034") as (target, _):
        with target.begin() as c:
            table = sa.Table("stores", sa.MetaData(), schema="public", autoload_with=c)
            c.execute(table.insert().values(**existing))
        result = run(target)
        assert result["success"] is False
        assert result["diagnostic"]["stage"] == "TARGET_PREFLIGHT"
        with target.connect() as c:
            assert transfer.count(c, "stores") == 1
            assert transfer.count(c, "products") == 0
    assert_clean(tmp_path)


def test_open_source_control_refuses(operator, source, tmp_path):
    run, _, statements, _ = operator
    with _isolated_database("0034") as (target, _):
        with source.begin() as c:
            c.exec_driver_sql("UPDATE public.routine_publishing_control SET state='LIVE'")
        try:
            result = run(target)
        finally:
            with source.begin() as c:
                c.exec_driver_sql("UPDATE public.routine_publishing_control SET state='PAUSED'")
        assert result["success"] is False
        assert result["diagnostic"]["stage"] == "EXECUTE_SINGLE_STATEMENT"
        assert statements == ["setup", "capture"]
        assert_empty(target)
    assert_clean(tmp_path)


def test_wrong_source_revision_refuses_before_target_access(operator, source, tmp_path):
    run, calls, statements, _ = operator
    with _isolated_database("0034") as (target, _):
        with source.begin() as c:
            c.exec_driver_sql("UPDATE public.alembic_version SET version_num='0030'")
        try:
            result = run(target)
        finally:
            with source.begin() as c:
                c.exec_driver_sql("UPDATE public.alembic_version SET version_num='0031'")
        assert result["success"] is False
        assert result["diagnostic"] == {
            "exception_class": "DivisionByZero", "sqlstate": "22012",
            "stage": "EXECUTE_SINGLE_STATEMENT"}
        assert calls == [True] and statements == ["setup", "capture"]
        assert_empty(target)
    assert_clean(tmp_path)


def test_source_capture_failure_is_safe_and_never_retried(operator, monkeypatch, tmp_path):
    run, _, _, _ = operator
    calls = []

    def unavailable(**kwargs):
        calls.append(True)
        return {"success": False, "diagnostic": {
            "stage": "EXECUTE_SINGLE_STATEMENT", "exception_class": "QueryCanceled",
            "sqlstate": "57014", "statement_position": 77}}

    monkeypatch.setattr(once, "capture_source_result", unavailable)
    with _isolated_database("0034") as (target, _):
        result = run(target)
        assert result["diagnostic"]["sqlstate"] == "57014"
        assert result["diagnostic"]["statement_position"] == 77
        assert result["success"] is False and calls == [True]
        assert_empty(target)
    assert_clean(tmp_path)


def test_target_wrong_schema_preflight_refuses(operator, tmp_path):
    run, _, _, _ = operator
    with _isolated_database("0033") as (target, _):
        result = run(target)
        assert result["success"] is False
        assert result["diagnostic"]["stage"] == "TARGET_PREFLIGHT"
        assert_empty(target)
    assert_clean(tmp_path)


def test_atomic_rollback_reaches_certification_after_inserts(operator, monkeypatch, tmp_path):
    run, _, _, _ = operator
    observed = []

    def fail_certification(c, bundle):
        observed.append(transfer.count(c, "products"))
        raise Refused(POISON)

    monkeypatch.setattr(transfer, "certify_connection", fail_certification)
    with _isolated_database("0034") as (target, _):
        result = run(target)
        assert result["success"] is False
        assert result["diagnostic"]["stage"] == "TARGET_IMPORT"
        assert result["import_committed"] is False
        assert observed == [2997]  # Prove the injected failure was after insertion.
        assert_empty(target)       # Independent durable-state read after rollback.
    assert POISON not in json.dumps(result)
    assert_clean(tmp_path)


@pytest.mark.parametrize("file", ["capsule.json", "bundle.json"])
def test_digest_tampering_refuses_and_cleans(operator, monkeypatch, tmp_path, file):
    run, _, _, _ = operator
    actual_capture = once.capture_source_result

    def corrupt(**kwargs):
        result = actual_capture(**kwargs)
        assert result["success"] is True
        path = kwargs["capsule_file"].parent / file
        data = json.loads(path.read_text())
        if file == "capsule.json":
            data["capsule_sha256"] = "0" * 64
        else:
            data["manifest"]["manifest_sha256"] = "0" * 64
        path.write_text(json.dumps(data))
        return result

    monkeypatch.setattr(once, "capture_source_result", corrupt)
    with _isolated_database("0034") as (target, _):
        result = run(target)
        assert result["success"] is False
        assert result["diagnostic"]["stage"] == "VERIFY_DIGESTS"
        assert_empty(target)
    assert_clean(tmp_path)


def test_private_files_overwritten_unlinked_on_preflight_error(operator, monkeypatch, tmp_path):
    run, _, _, _ = operator
    observed = []
    remove = once._remove_private

    def inspect(directory):
        assert stat.S_IMODE(directory.stat().st_mode) == 0o700
        for p in directory.iterdir():
            observed.append(p.name)
            assert stat.S_IMODE(p.stat().st_mode) == 0o600
        remove(directory)

    monkeypatch.setattr(once, "_remove_private", inspect)
    monkeypatch.setattr(once, "import_target", lambda *args, **kwargs: (_ for _ in ()).throw(PoisonError(POISON)))
    with _isolated_database("0034") as (target, _):
        result = run(target)
        assert result["diagnostic"]["stage"] == "TARGET_PREFLIGHT"
        assert POISON not in json.dumps(result)
        assert_empty(target)
    assert sorted(observed) == ["bundle.json", "capsule.json"]
    assert_clean(tmp_path)


def test_post_commit_certification_failure_reports_committed(operator, monkeypatch, tmp_path):
    run, _, _, _ = operator
    monkeypatch.setattr(once, "certify_target", lambda *args: (_ for _ in ()).throw(PoisonError(POISON)))
    with _isolated_database("0034") as (target, _):
        result = run(target)
        assert result["success"] is False and result["import_committed"] is True
        assert result["diagnostic"]["stage"] == "TARGET_CERTIFICATION"
        with target.connect() as c:
            assert transfer.count(c, "products") == 2997
    assert POISON not in json.dumps(result)
    assert_clean(tmp_path)


@pytest.mark.parametrize("missing", ["SOURCE_FIXTURE", "TARGET_FIXTURE"])
def test_missing_envs_refuse_before_capture(operator, monkeypatch, tmp_path, missing):
    _, calls, _, _ = operator
    monkeypatch.delenv(missing, raising=False)
    result = once.run_migration(source_env="SOURCE_FIXTURE", target_env="TARGET_FIXTURE",
                                execute=True, ephemeral_parent=tmp_path)
    assert result["success"] is False and result["diagnostic"]["stage"] == "CONFIGURATION"
    assert calls == []
    assert_clean(tmp_path)


def test_execution_gate_no_fallback_no_capture(operator, tmp_path):
    _, calls, _, _ = operator
    result = once.run_migration(source_env="SOURCE_FIXTURE", target_env="TARGET_FIXTURE",
                                ephemeral_parent=tmp_path)
    assert result["diagnostic"]["stage"] == "EXECUTION_GATE" and calls == []
    assert_clean(tmp_path)


def test_named_execution_ack_success(operator, monkeypatch, tmp_path):
    run, _, _, _ = operator
    monkeypatch.setenv("ONCE_FIXTURE_ACK", once.EXECUTION_ACK)
    with _isolated_database("0034") as (target, _):
        result = run(target, execute=False, execution_env="ONCE_FIXTURE_ACK")
        assert result["success"] is True
    assert_clean(tmp_path)


def test_same_endpoint_refuses_without_capture(operator, source, monkeypatch, tmp_path):
    _, calls, _, _ = operator
    monkeypatch.setenv("TARGET_FIXTURE", source.url.render_as_string(hide_password=False))
    result = once.run_migration(source_env="SOURCE_FIXTURE", target_env="TARGET_FIXTURE",
                                execute=True, ephemeral_parent=tmp_path)
    assert result["diagnostic"]["stage"] == "CONFIGURATION" and calls == []
    assert_clean(tmp_path)


def test_dsn_options_cannot_override_readonly(operator, source, monkeypatch, tmp_path):
    _, calls, _, _ = operator
    url = source.url.update_query_dict({"options": "-c default_transaction_read_only=off"})
    monkeypatch.setenv("SOURCE_FIXTURE", url.render_as_string(hide_password=False))
    monkeypatch.setenv("TARGET_FIXTURE", source.url.render_as_string(hide_password=False))
    result = once.run_migration(source_env="SOURCE_FIXTURE", target_env="TARGET_FIXTURE",
                                execute=True, ephemeral_parent=tmp_path)
    assert result["diagnostic"]["stage"] == "CONFIGURATION" and calls == []
    assert_clean(tmp_path)


def test_cleanup_failure_reports_commit_without_retry(operator, monkeypatch, tmp_path):
    run, calls, _, _ = operator
    remove = once._remove_private

    def failure_after_removal(directory):
        remove(directory)
        raise PoisonError(POISON)

    monkeypatch.setattr(once, "_remove_private", failure_after_removal)
    with _isolated_database("0034") as (target, _):
        result = run(target)
        assert result["success"] is False and result["import_committed"] is True
        assert result["ephemeral_cleanup"] == "FAIL"
        assert result["diagnostic"]["stage"] == "EPHEMERAL_CLEANUP"
        with target.connect() as c:
            assert transfer.count(c, "products") == 2997
    assert calls == [True] and POISON not in json.dumps(result)
    assert_clean(tmp_path)


def test_nonprivate_bundle_refuses_before_target(operator, monkeypatch, tmp_path):
    run, _, _, _ = operator
    capture = once.capture_source_result

    def loosen_permissions(**kwargs):
        result = capture(**kwargs)
        kwargs["bundle_file"].chmod(0o644)
        return result

    monkeypatch.setattr(once, "capture_source_result", loosen_permissions)
    with _isolated_database("0034") as (target, _):
        result = run(target)
        assert result["success"] is False and result["diagnostic"]["stage"] == "VERIFY_DIGESTS"
        assert_empty(target)
    assert_clean(tmp_path)


@pytest.mark.parametrize("statement,lock", [(999, 100), (900001, 100), (True, 100),
                                           (480000, 99), (480000, 60001), (1000, 2000)])
def test_bounded_timeouts_fail_before_capture(operator, tmp_path, statement, lock):
    _, calls, _, _ = operator
    result = once.run_migration(source_env="SOURCE_FIXTURE", target_env="TARGET_FIXTURE",
                                execute=True, ephemeral_parent=tmp_path,
                                statement_timeout_ms=statement, lock_timeout_ms=lock)
    assert result["diagnostic"]["stage"] == "CONFIGURATION" and calls == []
    assert_clean(tmp_path)


def test_cli_diagnostics_never_echo_arguments_or_env_values(capsys, monkeypatch):
    script = Path(__file__).resolve().parents[2] / "scripts/migrate_closed_state_once.py"
    spec = importlib.util.spec_from_file_location("migration_once_cli", script)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    assert cli.main(["--unknown", POISON]) == 2
    out = capsys.readouterr()
    assert out.err == "" and POISON not in out.out
    assert json.loads(out.out)["diagnostic"]["stage"] == "ARGUMENTS"
    monkeypatch.setenv("SOURCE_POISON", POISON)
    monkeypatch.setenv("TARGET_POISON", POISON)
    assert cli.main(["--execute", "--source-env", "SOURCE_POISON",
                     "--target-env", "TARGET_POISON"]) == 2
    out = capsys.readouterr()
    assert POISON not in out.out and out.err == ""