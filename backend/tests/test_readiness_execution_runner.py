import json
import os
import signal
import threading
import importlib.util

import pytest

from app.services import readiness_execution_runner as runner
from app.services.readiness_execution_contract import ReadinessError


_NOW = "2026-01-01T00:00:00+00:00"
_GATE = "OBJECT_STORAGE_READINESS_PROBE_ENABLED"


def _receipt(status="PASS", error_code=None):
    stages = (
        "gate", "key", "payload", "initialize", "preflight", "write", "read",
        "verify", "fresh_read", "fresh_verify", "delete", "absence",
    )
    failed_stage = "gate" if status == "BLOCKED" else "write"
    failed_index = stages.index(failed_stage)
    operations = {}
    for index, stage in enumerate(stages):
        if status == "PASS" or (status == "FAILED" and index < failed_index):
            operations[stage] = {
                "status": "PASS", "started_at": _NOW, "finished_at": _NOW,
            }
        elif index == failed_index:
            operations[stage] = {
                "status": "FAILED",
                "started_at": _NOW,
                "finished_at": _NOW,
                "error_code": error_code,
            }
        else:
            operations[stage] = {"status": "NOT_RUN"}
    key = "task61-readiness/" + ("a" * 32) + ".png"
    receipt = {
        "probe_version": "1",
        "object_key": key if status in {"PASS", "FAILED"} else None,
        "expected_digest": "dc5687b50f70cb95379bfced5a0eae768dd4382cd6b393ee77d65bbdd6373fbf",
        "byte_size": 70,
        "started_at": _NOW,
        "finished_at": _NOW,
        "operations": operations,
        "client_evidence": {
            "backend": "replit.object_storage",
            "constructor": "Client()",
            "default_bucket_only": True,
            "mode": "official_sdk",
            "clients_created": 3 if status == "PASS" else (1 if status == "FAILED" else 0),
            "fresh_read_distinct": status == "PASS",
            "absence_client_distinct": status == "PASS",
            "writer_bound_absence_verified": status == "PASS",
            "default_bucket_identity_verified": False,
            "upload_acknowledged": status == "PASS",
            "delete_acknowledged": status == "PASS",
            "published_runtime_marker": True,
            "production_runtime_certified": False,
        },
        "cleanup": (
            {
                "status": "CONFIRMED",
                "attempted": True,
                "via": "normal_sequence",
                "finished_at": _NOW,
            }
            if status == "PASS"
            else (
                {
                    "status": "FAILED",
                    "attempted": True,
                    "via": "finally",
                    "started_at": _NOW,
                    "upload_acknowledged": False,
                    "error_code": "UPLOAD_OUTCOME_UNCONFIRMED",
                    "finished_at": _NOW,
                }
                if status == "FAILED"
                else {"status": "NOT_NEEDED", "attempted": False}
            )
        ),
        "final_status": status,
    }
    if status != "PASS":
        receipt["error_code"] = error_code
    return receipt


class _FakePopen:
    def __init__(self, output=b"", *, returncode=0, terminate_exits=True):
        self._write_fd = None
        self._terminate_exits = terminate_exits
        self.returncode = returncode
        self.signals = []
        read_fd, write_fd = os.pipe()
        self.stdout = os.fdopen(read_fd, "rb", buffering=0)
        self._write_fd = write_fd
        if output:
            self._writer = threading.Thread(
                target=self._write_output, args=(output,), daemon=True
            )
            self._writer.start()
        else:
            self._writer = None

    def _write_output(self, data):
        try:
            view = memoryview(data)
            while view:
                written = os.write(self._write_fd, view[:4096])
                view = view[written:]
        except OSError:
            pass
        finally:
            self._close_write()

    def _close_write(self):
        if self._write_fd is not None:
            try:
                os.close(self._write_fd)
            except OSError:
                pass
            self._write_fd = None

    def poll(self):
        return self.returncode

    def send_signal(self, value):
        self.signals.append(value)
        if self._terminate_exits:
            self.returncode = -value
            self._close_write()

    def kill(self):
        self.signals.append("KILL")
        self.returncode = -signal.SIGKILL
        self._close_write()

    def wait(self, timeout=None):
        if self.returncode is None:
            raise runner.subprocess.TimeoutExpired("fake", timeout)
        return self.returncode


def _configure(monkeypatch, output, *, exit_code=0, terminate_exits=True):
    parent_env = {
        "PATH": "/usr/bin",
        "HOME": "/tmp/readiness-runner-test",
        "USER": "readiness-test",
        "LANG": "C",
        "PYTHONPATH": "backend",
        _GATE: "false",
    }
    monkeypatch.setattr(runner.os, "environ", parent_env)
    created = []

    def fake_popen(*args, **kwargs):
        process = _FakePopen(
            output, returncode=exit_code, terminate_exits=terminate_exits
        )
        created.append((args, kwargs, process))
        return process

    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    return parent_env, created


def test_launch_uses_fixed_script_and_child_only_opt_in(monkeypatch):
    parent, created = _configure(monkeypatch, json.dumps(_receipt()).encode())

    result = runner.launch_probe()

    assert result["outcome"] == "PASS"
    assert len(created) == 1
    args, kwargs, _ = created[0]
    assert args == ([runner.sys.executable, str(runner.PROBE_PATH)],)
    assert kwargs["shell"] is False
    assert kwargs["env"][_GATE] == "true"
    assert set(kwargs["env"]) == set(parent)
    assert parent[_GATE] == "false"
    assert kwargs["stdout"] == runner.subprocess.PIPE
    assert kwargs["stderr"] == runner.subprocess.DEVNULL


def test_open_parent_gate_is_rejected_without_launch(monkeypatch):
    parent = {_GATE: "true"}
    monkeypatch.setattr(runner.os, "environ", parent)
    monkeypatch.setattr(
        runner.subprocess, "Popen",
        lambda *args, **kwargs: pytest.fail("probe must not launch"),
    )

    with pytest.raises(ReadinessError) as error:
        runner.launch_probe()

    assert error.value.code == "PARENT_GATE_NOT_CLOSED"


@pytest.mark.parametrize(
    ("status", "code", "exit_code", "outcome"),
    [
        ("BLOCKED", "GATE_DISABLED", 2, "FAILED"),
        ("FAILED", "SDK_OPERATION_FAILED", 1, "FAILED"),
    ],
)
def test_known_blocked_and_failed_receipts_are_safely_returned(
    monkeypatch, status, code, exit_code, outcome
):
    payload = json.dumps(_receipt(status, code)).encode()
    _configure(monkeypatch, payload, exit_code=exit_code)

    result = runner.launch_probe()

    assert result["outcome"] == outcome
    assert result["receipt"]["final_status"] == status
    assert result["receipt"]["error_code"] == code


@pytest.mark.parametrize(
    "corruption",
    [
        lambda r: r["client_evidence"].update(mode="injected_test"),
        lambda r: r["client_evidence"].update(fresh_read_distinct=False),
        lambda r: r["client_evidence"].update(production_runtime_certified=True),
        lambda r: r.update(cleanup={
            "status": "FAILED",
            "attempted": True,
            "via": "finally",
            "started_at": _NOW,
            "upload_acknowledged": True,
            "delete_result": "PASS",
            "absence_result": "PASS",
            "error_code": "SDK_OPERATION_FAILED",
            "finished_at": _NOW,
        }),
        lambda r: r.update(secret_value="must-not-escape"),
        lambda r: r["object_key"].replace("a" * 32, "z" * 32),
    ],
)
def test_forged_or_corrupted_pass_is_unknown_without_receipt(monkeypatch, corruption):
    receipt = _receipt()
    changed = corruption(receipt)
    if isinstance(changed, str):
        receipt["object_key"] = changed
    _configure(monkeypatch, json.dumps(receipt).encode())

    result = runner.launch_probe()

    assert result["outcome"] == "UNKNOWN"
    assert result["receipt"] is None
    assert result["error_code"] == "PROBE_RECEIPT_INVALID"


def test_duplicate_json_keys_and_untrusted_output_are_not_returned_or_logged(
    monkeypatch, caplog
):
    raw = b'{"final_status":"PASS","final_status":"PASS","private":"provider-secret"}'
    _configure(monkeypatch, raw)

    result = runner.launch_probe()

    assert result["outcome"] == "UNKNOWN"
    assert result["receipt"] is None
    assert "provider-secret" not in repr(result)
    assert "provider-secret" not in caplog.text


def test_exit_code_must_match_receipt_terminal_state(monkeypatch):
    _configure(monkeypatch, json.dumps(_receipt()).encode(), exit_code=1)

    result = runner.launch_probe()

    assert result == {
        "outcome": "UNKNOWN",
        "exit_code": 1,
        "receipt": None,
        "error_code": "PROBE_EXIT_MISMATCH",
    }


@pytest.mark.parametrize("force_kill", [False, True])
def test_timeout_sends_term_then_kill_only_if_needed(monkeypatch, force_kill):
    process = None

    def fake_popen(*args, **kwargs):
        nonlocal process
        process = _FakePopen(
            returncode=None, terminate_exits=not force_kill
        )
        return process

    monkeypatch.setattr(runner.os, "environ", {_GATE: "false"})
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)
    monkeypatch.setattr(runner, "PROBE_TIMEOUT_SECONDS", 0.02)
    monkeypatch.setattr(runner, "TERMINATION_GRACE_SECONDS", 0.01)

    result = runner.launch_probe()

    assert result["outcome"] == "UNKNOWN"
    assert result["receipt"] is None
    assert result["error_code"] == "PROBE_TIMEOUT"
    assert process.signals[0] == signal.SIGTERM
    assert ("KILL" in process.signals) is force_kill


def test_cancellation_terminates_child_and_propagates(monkeypatch):
    process = None

    def fake_popen(*args, **kwargs):
        nonlocal process
        process = _FakePopen(returncode=None)
        return process

    monkeypatch.setattr(runner.os, "environ", {_GATE: "false"})
    monkeypatch.setattr(runner.subprocess, "Popen", fake_popen)

    def cancelled(_process):
        raise KeyboardInterrupt()

    monkeypatch.setattr(runner, "_read_child_output", cancelled)
    with pytest.raises(KeyboardInterrupt):
        runner.launch_probe()

    assert process.signals == [signal.SIGTERM]


def test_stdout_over_limit_fails_closed(monkeypatch):
    _configure(monkeypatch, b" " * (runner.MAX_STDOUT_BYTES + 1))

    result = runner.launch_probe()

    assert result["outcome"] == "UNKNOWN"
    assert result["receipt"] is None
    assert result["error_code"] == "PROBE_OUTPUT_TOO_LARGE"


def test_validate_receipt_rejects_unknown_fields_and_mismatched_exit():
    receipt = _receipt()
    receipt["unexpected"] = "sensitive"

    with pytest.raises(ReadinessError):
        runner.validate_receipt(receipt, 0)
    with pytest.raises(ReadinessError):
        runner.validate_receipt(_receipt(), 1)


def test_real_probe_injected_fake_sdk_receipt_is_validated_without_live_sdk():
    spec = importlib.util.spec_from_file_location(
        "readiness_probe_test_module", runner.PROBE_PATH
    )
    probe = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(probe)
    storage = {}

    class FakeObjectNotFoundError(Exception):
        pass

    class FakeClient:
        def exists(self, key):
            return key in storage

        def upload_from_bytes(self, key, content):
            storage[key] = content

        def download_as_bytes(self, key):
            try:
                return storage[key]
            except KeyError:
                raise FakeObjectNotFoundError() from None

        def delete(self, key):
            if key not in storage:
                raise FakeObjectNotFoundError()
            del storage[key]

    def fake_sdk_loader():
        return FakeClient, FakeObjectNotFoundError

    receipt = probe.run_probe(
        environ={_GATE: "true"},
        sdk_loader=fake_sdk_loader,
    )

    assert receipt["final_status"] == "PASS"
    assert receipt["client_evidence"]["mode"] == "injected_test"
    assert receipt["client_evidence"]["published_runtime_marker"] is False
    assert storage == {}
    with pytest.raises(ReadinessError):
        runner.validate_receipt(receipt, 0)

    # This is a validation-only fixture: no CLI and no official SDK client.
    official_marker_fixture = dict(receipt)
    official_marker_fixture["client_evidence"] = dict(receipt["client_evidence"])
    official_marker_fixture["client_evidence"]["mode"] = "official_sdk"
    official_marker_fixture["client_evidence"]["published_runtime_marker"] = True
    assert runner.validate_receipt(official_marker_fixture, 0)["final_status"] == "PASS"


def test_failure_receipts_allow_interrupt_without_failed_stage_and_four_clients():
    interrupted_between_stages = _receipt()
    interrupted_between_stages["final_status"] = "FAILED"
    interrupted_between_stages["error_code"] = "INTERRUPTED"
    assert runner.validate_receipt(interrupted_between_stages, 1)["final_status"] == "FAILED"

    absence_cleanup_retry = _receipt()
    absence_cleanup_retry["final_status"] = "FAILED"
    absence_cleanup_retry["error_code"] = "ABSENCE_NOT_CONFIRMED"
    absence_cleanup_retry["operations"]["absence"] = {
        "status": "FAILED",
        "started_at": _NOW,
        "finished_at": _NOW,
        "error_code": "ABSENCE_NOT_CONFIRMED",
    }
    absence_cleanup_retry["client_evidence"]["clients_created"] = 4
    absence_cleanup_retry["cleanup"] = {
        "status": "CONFIRMED",
        "attempted": True,
        "via": "finally",
        "started_at": _NOW,
        "upload_acknowledged": True,
        "delete_result": "PASS",
        "absence_result": "PASS",
        "finished_at": _NOW,
    }
    assert runner.validate_receipt(absence_cleanup_retry, 1)["final_status"] == "FAILED"


def test_pass_requires_normal_sequence_cleanup_and_exactly_three_clients():
    receipt = _receipt()
    receipt["cleanup"] = {
        "status": "CONFIRMED",
        "attempted": True,
        "via": "finally",
        "started_at": _NOW,
        "upload_acknowledged": True,
        "delete_result": "PASS",
        "absence_result": "PASS",
        "finished_at": _NOW,
    }
    with pytest.raises(ReadinessError):
        runner.validate_receipt(receipt, 0)

    receipt = _receipt()
    receipt["client_evidence"]["clients_created"] = 4
    with pytest.raises(ReadinessError):
        runner.validate_receipt(receipt, 0)