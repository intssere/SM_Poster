"""Failure-boundary regressions with an explicitly fake coordinator and child."""
from __future__ import annotations

import time
from types import SimpleNamespace

import pytest

from app.core.config import Settings
from app.services import readiness_execution as execution
from app.services import readiness_execution_admission as admission
from app.services import readiness_execution_runner as runner
from app.services.readiness_execution_contract import ReadinessBinding, ReadinessError


@pytest.fixture
def operation(monkeypatch):
    state = SimpleNamespace(consumed=False, launches=0, finish_calls=[], counters={"attempts": 0})
    state.settings = Settings(_env_file=None, database_url="sqlite:///:memory:")
    state.binding = ReadinessBinding("a"*40, "b"*40, "c"*40, "d"*40,
                                     "e"*64, "f"*64, "canonical_parent_with_checkpoint_overlay")
    state.claims = {"jti": "1"*64, "actor": "2"*64, "iat": int(time.time())-1,
                    "exp": int(time.time())+120}
    class Store:
        def __init__(self, engine):
            pass
        def consume(self, binding, grant_id, actor, *, expires_at=None):
            if state.consumed:
                return False
            state.consumed = True
            return True
        def finish(self, binding, outcome, exit_code, receipt):
            state.finish_calls.append((outcome, exit_code, receipt))
        def lookup(self, binding):
            if not state.consumed:
                return None
            outcome, code, receipt = state.finish_calls[-1] if state.finish_calls else (
                "UNKNOWN", None, None,
            )
            return {"admission_state": "CONSUMED", "outcome": outcome,
                    "exit_code": code, "receipt": receipt}

    def launch():
        state.launches += 1
        return {"outcome": "UNKNOWN", "exit_code": -9, "receipt": None,
                "error_code": "PROBE_TIMEOUT"}
    state.Store = Store
    monkeypatch.setattr(admission, "PostgresReadinessAdmission", Store)
    monkeypatch.setattr(execution, "require_static_safety", lambda *_a, **_kw: None)
    monkeypatch.setattr(execution, "require_runtime_binding", lambda *_: None)
    monkeypatch.setattr(execution, "require_scheduler_stopped", lambda *_: None)
    monkeypatch.setattr(execution, "execution_engine", lambda: object())
    monkeypatch.setattr(execution, "business_snapshot", lambda *_: dict(state.counters))
    monkeypatch.setattr(runner, "launch_probe", launch)
    return state


def test_coordinator_failure_does_not_launch(operation, monkeypatch):
    def fail(*_, **_kwargs):
        raise ReadinessError("COORDINATOR_UNAVAILABLE")
    monkeypatch.setattr(operation.Store, "consume", fail)
    with pytest.raises(ReadinessError, match="COORDINATOR_UNAVAILABLE"):
        execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    assert operation.launches == 0 and not operation.finish_calls


def test_expired_grant_before_thread_starts_does_not_consume(operation):
    operation.claims["exp"] = 1
    with pytest.raises(ReadinessError, match="EXECUTION_AUTHORIZATION_EXPIRED"):
        execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    assert not operation.consumed and operation.launches == 0


def test_grant_expiring_during_admission_stays_consumed(operation, monkeypatch):
    consume = operation.Store.consume
    def expire(store, *_args, **kwargs):
        result = consume(store, *_args, **kwargs)
        operation.claims["exp"] = 1
        return result
    monkeypatch.setattr(operation.Store, "consume", expire)
    result = execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    assert result["outcome"] == "UNKNOWN" and operation.consumed
    assert operation.launches == 0
    assert operation.finish_calls == [("UNKNOWN", None, None)]


def test_grant_expiring_during_final_baseline_stays_consumed(operation, monkeypatch):
    snapshots = []
    def snapshot(*_):
        snapshots.append(True)
        if len(snapshots) == 2:
            operation.claims["exp"] = 1
        return dict(operation.counters)
    monkeypatch.setattr(execution, "business_snapshot", snapshot)
    result = execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    assert result["outcome"] == "UNKNOWN"
    assert result["error_code"] == "EXECUTION_AUTHORIZATION_EXPIRED"
    assert operation.consumed and operation.launches == 0
    assert operation.finish_calls == [("UNKNOWN", None, None)]


def test_spawn_failure_cannot_rearm(operation, monkeypatch):
    def fail():
        operation.launches += 1
        raise OSError("synthetic secret-bearing exception; must not escape")
    monkeypatch.setattr(runner, "launch_probe", fail)
    result = execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    assert result["outcome"] == "UNKNOWN" and result["receipt"] is None
    with pytest.raises(ReadinessError, match="ADMISSION_ALREADY_CONSUMED"):
        execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    assert operation.launches == 1


def test_outcome_persistence_failure_does_not_relaunch(operation, monkeypatch):
    def fail(*_):
        raise ReadinessError("OUTCOME_PERSISTENCE_FAILED")
    monkeypatch.setattr(operation.Store, "finish", fail)
    with pytest.raises(ReadinessError, match="OUTCOME_PERSISTENCE_FAILED"):
        execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    with pytest.raises(ReadinessError, match="ADMISSION_ALREADY_CONSUMED"):
        execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    assert operation.consumed and operation.launches == 1
    assert execution.lookup_readiness(operation.binding)["outcome"] == "UNKNOWN"


def test_post_probe_baseline_change_never_reports_overall_pass(operation, monkeypatch):
    def launch():
        operation.launches += 1
        operation.counters["attempts"] = 1
        return {"outcome": "PASS", "exit_code": 0, "receipt": None}
    monkeypatch.setattr(runner, "launch_probe", launch)
    result = execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    assert result["outcome"] == "UNKNOWN"
    assert result["error_code"] == "BUSINESS_BASELINE_CHANGED"
    assert operation.consumed and operation.launches == 1


def test_gate_change_after_consumption_prevents_launch(operation, monkeypatch):
    calls = []
    def safety(*_a, **_kw):
        calls.append(True)
        if len(calls) == 2:
            raise ReadinessError("OPERATIONAL_GATES_NOT_CLOSED")
    monkeypatch.setattr(execution, "require_static_safety", safety)
    result = execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    assert result["outcome"] == "UNKNOWN" and operation.consumed
    assert operation.launches == 0


def test_invalid_stored_receipt_is_never_returned(operation):
    operation.consumed = True
    operation.finish_calls.append(("PASS", 0, {"credentials": "synthetic-do-not-return"}))
    with pytest.raises(ReadinessError, match="STORED_RECEIPT_INVALID"):
        execution.lookup_readiness(operation.binding)


@pytest.mark.parametrize("code", [0, 1, None])
def test_missing_stored_pass_evidence_never_reports_success(operation, code):
    operation.consumed = True
    operation.finish_calls.append(("PASS", code, None))
    with pytest.raises(ReadinessError, match="STORED_RECEIPT_INVALID"):
        execution.lookup_readiness(operation.binding)


@pytest.mark.parametrize("disabled_before", [False, True])
def test_sanitized_logging_on_unknown(operation, caplog, monkeypatch, disabled_before):
    # In-process Alembic fileConfig() in broader suites disables existing
    # loggers. Own this test's logging state and restore it during teardown.
    monkeypatch.setattr(execution.logger, "disabled", disabled_before)
    monkeypatch.setattr(execution.logger, "disabled", False)
    monkeypatch.setattr(execution.logger, "propagate", True)
    with caplog.at_level("INFO", logger=execution.logger.name):
        execution.execute_readiness(operation.settings, operation.binding, operation.claims)
    assert "admission-consumed" in caplog.text
    assert "outcome=UNKNOWN" in caplog.text
    assert operation.claims["jti"] not in caplog.text
    assert operation.claims["actor"] not in caplog.text


def test_read_only_status_never_launches(operation):
    assert execution.lookup_readiness(operation.binding)["outcome"] == "NOT_RUN"
    operation.consumed = True
    result = execution.lookup_readiness(operation.binding)
    assert result["outcome"] == "UNKNOWN" and result["admission_state"] == "CONSUMED"
    assert operation.launches == 0