"""Task 61.53: one-shot closed-state bounded preparation CLI regressions."""
from __future__ import annotations

import inspect
import json

import sqlalchemy as sa
from sqlalchemy.orm import sessionmaker

from app.core.config import Settings
from app.models.domain import AuditLog
from app.state_transfer import prepare_bounded_pilot as cli


def _settings(**changes):
    base = {
        "DATABASE_URL": "sqlite:///:memory:",
        "PUBLISHING_ENABLED": False,
        "BUFFER_PUBLISHING_ENABLED": False,
        "ROUTINE_PINTEREST_WORKER_ENABLED": False,
        "ROUTINE_BUFFER_DISPATCH_ENABLED": False,
        "ROUTINE_SCHEDULED_LIVE_ADMISSION_ENABLED": False,
        "ROUTINE_BOUNDED_BATCH_ENABLED": False,
        "ROUTINE_PINTEREST_DRY_RUN": True,
        "ROUTINE_PINTEREST_BATCH_SIZE": 1,
        "ROUTINE_PINTEREST_DAILY_WRITE_LIMIT": 1,
    }
    base.update(changes)
    return Settings(_env_file=None, **base)


def _preflight():
    candidates = []
    for index in range(5):
        candidates.append({
            "item_id": f"item-{index}",
            "item_fingerprint": f"{100 + index:064x}",
            "candidate_fingerprint": f"{200 + index:064x}",
            "product_id": f"product-{index}",
            "local_board_id": "local-board",
            "pinterest_board_record_id": "provider-board",
            "external_board_id": "external-board",
            "content_angle_id": "angle",
            "planned_date": f"2026-10-{10 + index:02d}",
            "slot_index": index,
            "candidate_identity_fingerprint": f"{300 + index:064x}",
        })
    return {
        "success": True,
        "bounded_preflight_certification": "PASS",
        "publishing_admission": "NOT_GRANTED",
        "database_revision": "0034",
        "month_start": "2026-10-01",
        "current_date": "2026-10-06",
        "plan_id": "plan",
        "plan_fingerprint": "1" * 64,
        "candidate_count": 5,
        "candidates": candidates,
        "preflight_fingerprint": "2" * 64,
    }


class FakeSession:
    def __init__(self):
        self.closed = False

    def close(self):
        self.closed = True


def _prepared(receipt):
    return {
        "success": True,
        "status": "READY",
        "batch_id": "batch-id",
        "batch_manifest_sha256": "3" * 64,
        "preflight_fingerprint": receipt["preflight_fingerprint"],
        "plan_id": receipt["plan_id"],
        "plan_fingerprint": receipt["plan_fingerprint"],
        "candidate_count": 5,
        "entries": [{"slot": i, "item_id": f"item-{i}"} for i in range(5)],
        "provider_calls": 0,
        "buffer_calls": 0,
        "pinterest_calls": 0,
        "oauth_calls": 0,
        "ai_calls": 0,
        "publishing_admission": "NOT_GRANTED",
        "idempotent": False,
    }


def test_one_shot_preflight_then_process_local_override_only(monkeypatch):
    persistent = _settings()
    session = FakeSession()
    captured = {}

    monkeypatch.setattr(cli, "gate_snapshot", lambda: {
        "ROUTINE_BOUNDED_BATCH_ENABLED": False,
        "ROUTINE_PINTEREST_DRY_RUN": True,
        "ROUTINE_PINTEREST_BATCH_SIZE": 1,
        "ROUTINE_PINTEREST_DAILY_WRITE_LIMIT": 1,
    })

    def prepare(db, *, settings, actor, receipt, renderer=None):
        captured.update(
            db=db,
            settings=settings,
            actor=actor,
            receipt=receipt,
            renderer=renderer,
        )
        return _prepared(receipt)

    monkeypatch.setattr(cli, "prepare_certified_batch", prepare)
    result = cli.run(
        preflight_runner=_preflight,
        settings_factory=lambda: persistent,
        session_factory=lambda: session,
    )

    assert result["success"] is True
    assert result["terminal_stage"] == "COMPLETE"
    assert result["code"] == "READY"
    assert result["batch_id"] == "batch-id"
    assert result["candidate_count"] == 5
    assert result["process_local_bounded_override"] is True
    assert result["persistent_configuration_mutated"] is False
    assert result["publishing_admission"] == "NOT_GRANTED"
    assert session.closed is True

    scoped = captured["settings"]
    assert captured["actor"] == cli.ACTOR
    assert captured["receipt"]["preflight_fingerprint"] == "2" * 64
    assert scoped.routine_bounded_batch_enabled is True
    assert scoped.routine_pinterest_batch_size == 5
    assert scoped.routine_pinterest_daily_write_limit == 5
    assert scoped.routine_pinterest_dry_run is True
    assert scoped.publishing_enabled is False
    assert scoped.buffer_publishing_enabled is False
    assert scoped.routine_pinterest_worker_enabled is False
    assert scoped.routine_buffer_dispatch_enabled is False
    assert scoped.routine_scheduled_live_admission_enabled is False
    assert scoped.routine_pinterest_scheduler_enabled is False
    assert scoped.routine_scheduled_autonomy_enabled is False
    assert scoped.pinterest_write_scope_enabled is False
    assert scoped.pinterest_board_write_scope_enabled is False
    assert scoped.pinterest_board_provisioning_enabled is False
    assert scoped.pinterest_autonomous_board_ensure_enabled is False

    # model_copy must not mutate the real environment-derived settings object.
    assert persistent.routine_bounded_batch_enabled is False
    assert persistent.routine_pinterest_batch_size == 1
    assert persistent.routine_pinterest_daily_write_limit == 1


def test_restart_guard_consumes_authorization_before_preflight_and_blocks_restart():
    engine = sa.create_engine("sqlite:///:memory:")
    AuditLog.__table__.create(engine)
    sessions = sessionmaker(bind=engine, expire_on_commit=False)
    calls = []
    failed = {
        "success": False,
        "bounded_preflight_certification": "NOT_GRANTED",
        "publishing_admission": "NOT_GRANTED",
    }
    invocation_id = "a" * 64

    first = cli.run(
        preflight_runner=lambda: (calls.append("preflight") or failed),
        invocation_id=invocation_id,
        invocation_session_factory=sessions,
        require_invocation_guard=True,
    )
    assert first["success"] is False
    assert first["code"] == "PREFLIGHT_NOT_CERTIFIED"
    assert first["invocation_guard_claimed"] is True
    assert first["invocation_guard_database_writes"] == 1
    assert first["invocation_receipt_id"] == cli._invocation_receipt_id(invocation_id)
    assert calls == ["preflight"]

    second = cli.run(
        preflight_runner=lambda: (calls.append("restart-preflight") or failed),
        invocation_id=invocation_id,
        invocation_session_factory=sessions,
        require_invocation_guard=True,
    )
    assert second["success"] is False
    assert second["terminal_stage"] == "INVOCATION_GUARD"
    assert second["code"] == "PREPARATION_INVOCATION_ALREADY_CLAIMED"
    assert second["invocation_guard_claimed"] is False
    assert second["invocation_guard_database_writes"] == 0
    assert calls == ["preflight"]

    with sessions() as db:
        rows = list(db.scalars(sa.select(AuditLog)).all())
        assert len(rows) == 1
        assert rows[0].action == cli.INVOCATION_ACTION
        assert rows[0].entity_id == invocation_id
        assert rows[0].correlation_id == invocation_id
    engine.dispose()


def test_restart_guard_requires_exact_hex64_invocation_id():
    for value, code in (
        (None, "PREPARATION_INVOCATION_ID_REQUIRED"),
        ("not-a-valid-id", "PREPARATION_INVOCATION_ID_INVALID"),
    ):
        result = cli.run(
            preflight_runner=lambda: (_ for _ in ()).throw(
                AssertionError("preflight must not run before invocation guard")
            ),
            invocation_id=value,
            require_invocation_guard=True,
        )
        assert result["success"] is False
        assert result["terminal_stage"] == "INVOCATION_GUARD"
        assert result["code"] == code


def test_restart_safe_runtime_always_starts_api_after_refusal(monkeypatch, capsys):
    from app.state_transfer import prepare_bounded_pilot_runtime as runtime

    invocation_id = "b" * 64
    monkeypatch.setenv(cli.INVOCATION_ENV, invocation_id)
    calls = []

    def preparation_runner(**kwargs):
        calls.append(("prepare", kwargs))
        result = cli._base_result()
        result.update(
            terminal_stage="INVOCATION_GUARD",
            code="PREPARATION_INVOCATION_ALREADY_CLAIMED",
        )
        return result

    def server_runner(app, **kwargs):
        calls.append(("server", app, kwargs))

    assert runtime.main(
        preparation_runner=preparation_runner,
        server_runner=server_runner,
    ) == 0
    assert calls[0] == (
        "prepare",
        {
            "invocation_id": invocation_id,
            "require_invocation_guard": True,
        },
    )
    assert calls[1] == (
        "server",
        "app.main:app",
        {"host": "0.0.0.0", "port": 8000},
    )
    output = json.loads(capsys.readouterr().out)
    assert output["code"] == "PREPARATION_INVOCATION_ALREADY_CLAIMED"
    assert output["publishing_admission"] == "NOT_GRANTED"


def test_restart_safe_runtime_sanitizes_exception_and_starts_api(monkeypatch, capsys):
    from app.state_transfer import prepare_bounded_pilot_runtime as runtime

    monkeypatch.setenv(cli.INVOCATION_ENV, "c" * 64)
    calls = []

    def fail(**kwargs):
        raise RuntimeError("PRIVATE_DATABASE_URL_AND_TOKEN")

    def server_runner(app, **kwargs):
        calls.append((app, kwargs))

    assert runtime.main(
        preparation_runner=fail,
        server_runner=server_runner,
    ) == 0
    rendered = capsys.readouterr().out
    assert "PRIVATE_DATABASE_URL_AND_TOKEN" not in rendered
    result = json.loads(rendered)
    assert result["terminal_stage"] == "RUNTIME_WRAPPER"
    assert result["code"] == "BOUNDED_PREPARATION_UNEXPECTED_ERROR"
    assert calls == [("app.main:app", {"host": "0.0.0.0", "port": 8000})]


def test_failed_preflight_never_opens_mutating_session(monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "gate_snapshot", lambda: (_ for _ in ()).throw(
        AssertionError("closed-state recheck must not run after failed preflight")
    ))
    monkeypatch.setattr(cli, "prepare_certified_batch", lambda *a, **k: (_ for _ in ()).throw(
        AssertionError("preparation must not run")
    ))
    failed = {
        "success": False,
        "bounded_preflight_certification": "NOT_GRANTED",
        "publishing_admission": "NOT_GRANTED",
    }
    result = cli.run(
        preflight_runner=lambda: failed,
        settings_factory=lambda: _settings(),
        session_factory=lambda: opened.append(1),
    )
    assert result["success"] is False
    assert result["code"] == "PREFLIGHT_NOT_CERTIFIED"
    assert result["terminal_stage"] == "PREFLIGHT"
    assert opened == []


def test_persistent_closed_state_drift_blocks_before_session(monkeypatch):
    opened = []
    monkeypatch.setattr(cli, "gate_snapshot", lambda: {
        "ROUTINE_BOUNDED_BATCH_ENABLED": False,
        "ROUTINE_PINTEREST_DRY_RUN": True,
        "ROUTINE_PINTEREST_BATCH_SIZE": 1,
        "ROUTINE_PINTEREST_DAILY_WRITE_LIMIT": 1,
    })
    result = cli.run(
        preflight_runner=_preflight,
        settings_factory=lambda: _settings(PUBLISHING_ENABLED=True),
        session_factory=lambda: opened.append(1),
    )
    assert result["success"] is False
    assert result["terminal_stage"] == "CLOSED_STATE_RECHECK"
    assert result["code"] == "PERSISTENT_CLOSED_STATE_DRIFT"
    assert opened == []


def test_operator_failure_is_sanitized_and_session_closed(monkeypatch):
    from app.services.bounded_pilot_preparation_operator import BoundedPreparationOperatorError

    session = FakeSession()
    monkeypatch.setattr(cli, "gate_snapshot", lambda: {"closed": True})

    def fail(*args, **kwargs):
        raise BoundedPreparationOperatorError("BOUNDED_PREPARATION_PLAN_DRIFT")

    monkeypatch.setattr(cli, "prepare_certified_batch", fail)
    result = cli.run(
        preflight_runner=_preflight,
        settings_factory=lambda: _settings(),
        session_factory=lambda: session,
    )
    assert result["success"] is False
    assert result["code"] == "BOUNDED_PREPARATION_PLAN_DRIFT"
    assert result["publishing_admission"] == "NOT_GRANTED"
    assert session.closed is True


def test_unexpected_failure_does_not_echo_exception(monkeypatch):
    session = FakeSession()
    monkeypatch.setattr(cli, "gate_snapshot", lambda: {"closed": True})

    def fail(*args, **kwargs):
        raise RuntimeError("PRIVATE_DATABASE_URL_AND_TOKEN")

    monkeypatch.setattr(cli, "prepare_certified_batch", fail)
    result = cli.run(
        preflight_runner=_preflight,
        settings_factory=lambda: _settings(),
        session_factory=lambda: session,
    )
    rendered = json.dumps(result, sort_keys=True)
    assert result["success"] is False
    assert result["code"] == "BOUNDED_PREPARATION_UNEXPECTED_ERROR"
    assert "PRIVATE_DATABASE_URL_AND_TOKEN" not in rendered
    assert session.closed is True


def test_cli_arguments_are_prohibited_without_echo(monkeypatch, capsys):
    private = "PRIVATE_ARGUMENT_SENTINEL"
    assert cli.main(["--receipt", private]) == 2
    output = capsys.readouterr()
    assert output.err == ""
    assert private not in output.out
    result = json.loads(output.out)
    assert result["terminal_stage"] == "ARGUMENTS"
    assert result["code"] == "ARGUMENTS_PROHIBITED"
    assert result["persistent_configuration_mutated"] is False
    assert result["publishing_admission"] == "NOT_GRANTED"


def test_source_has_no_persistent_config_or_provider_dispatch_controls():
    source = inspect.getsource(cli)
    for forbidden in (
        "set_variables",
        "update_service",
        "BufferGateway",
        "PinterestClient",
        "create_pinterest_post",
        "run_bounded_batch_once",
        "set_control",
        "os.environ[",
    ):
        assert forbidden not in source
    assert "model_copy(update=" in source
    assert '"routine_bounded_batch_enabled": True' in source
    assert '"routine_pinterest_batch_size": 5' in source
    assert '"routine_pinterest_daily_write_limit": 5' in source
    assert '"publishing_enabled": False' in source
    assert '"buffer_publishing_enabled": False' in source
