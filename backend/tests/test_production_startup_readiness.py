from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest


SCRIPT_PATH = (
    Path(__file__).parents[2]
    / "scripts"
    / "start_production.py"
)
spec = importlib.util.spec_from_file_location("production_startup_readiness", SCRIPT_PATH)
assert spec and spec.loader
startup = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = startup
spec.loader.exec_module(startup)


class FakeResponse:
    def __init__(self, payload):
        self._raw = json.dumps(payload).encode("utf-8")

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False

    def read(self):
        return self._raw


class FakeProcess:
    def __init__(self, poll_values=None):
        self.poll_values = list(poll_values or [])
        self.terminated = False
        self.killed = False
        self.waited = False

    def poll(self):
        if self.poll_values:
            return self.poll_values.pop(0)
        return None

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.waited = True
        return 0


class Clock:
    def __init__(self):
        self.value = 0.0

    def monotonic(self):
        return self.value

    def sleep(self, seconds):
        self.value += seconds


def _lifecycle_events(output: str) -> list[str]:
    events = []
    for line in output.splitlines():
        if not line.startswith(f"{startup.LOG_PREFIX} event="):
            continue
        event_token = line.split()[1]
        events.append(event_token.split("=", 1)[1])
    return events


def test_probe_requires_ok_and_database_connected_true():
    def opener(_url, timeout):
        assert timeout == 2.0
        return FakeResponse({"status": "ok", "database_connected": True})

    assert startup.probe_backend_ready(opener=opener) is True

    for payload in (
        {"status": "degraded", "database_connected": False},
        {"status": "ok", "database_connected": False},
        {"status": "ok"},
    ):
        assert (
            startup.probe_backend_ready(
                opener=lambda _url, timeout, payload=payload: FakeResponse(payload)
            )
            is False
        )


def test_wait_for_backend_ready_succeeds_after_strict_probe_passes():
    process = FakeProcess()
    clock = Clock()
    outcomes = iter([False, False, True])

    startup.wait_for_backend_ready(
        process,
        timeout_seconds=10,
        poll_interval_seconds=1,
        probe=lambda: next(outcomes),
        monotonic=clock.monotonic,
        sleep=clock.sleep,
    )

    assert clock.value == 2.0


def test_wait_for_backend_ready_times_out_fail_closed():
    process = FakeProcess()
    clock = Clock()

    with pytest.raises(startup.StartupError, match="timed out"):
        startup.wait_for_backend_ready(
            process,
            timeout_seconds=2,
            poll_interval_seconds=1,
            probe=lambda: False,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


def test_wait_for_backend_ready_rejects_backend_exit():
    process = FakeProcess(poll_values=[17])
    clock = Clock()

    with pytest.raises(startup.StartupError, match="exited before readiness"):
        startup.wait_for_backend_ready(
            process,
            timeout_seconds=10,
            probe=lambda: True,
            monotonic=clock.monotonic,
            sleep=clock.sleep,
        )


def test_run_starts_frontend_only_after_backend_readiness(monkeypatch):
    events = []
    backend = FakeProcess()
    frontend = FakeProcess()

    monkeypatch.setattr(
        startup,
        "start_backend",
        lambda: events.append("backend") or backend,
    )

    def ready(process, *, timeout_seconds):
        assert process is backend
        assert timeout_seconds == startup.DEFAULT_READY_TIMEOUT_SECONDS
        events.append("ready")

    monkeypatch.setattr(startup, "wait_for_backend_ready", ready)
    monkeypatch.setattr(
        startup,
        "start_frontend",
        lambda: events.append("frontend") or frontend,
    )
    monkeypatch.setattr(
        startup,
        "supervise",
        lambda b, f: events.append("supervise") or 0,
    )

    assert startup.run() == 0
    assert events == ["backend", "ready", "frontend", "supervise"]


def test_run_never_starts_frontend_when_readiness_fails(monkeypatch):
    events = []
    backend = FakeProcess()

    monkeypatch.setattr(
        startup,
        "start_backend",
        lambda: events.append("backend") or backend,
    )

    def refuse(_process, *, timeout_seconds):
        events.append("readiness_failed")
        raise startup.StartupError("not ready")

    monkeypatch.setattr(startup, "wait_for_backend_ready", refuse)
    monkeypatch.setattr(
        startup,
        "start_frontend",
        lambda: pytest.fail("frontend must not start before readiness"),
    )

    assert startup.run() == 1
    assert events == ["backend", "readiness_failed"]
    assert backend.terminated is True


def test_lifecycle_log_records_monotonic_elapsed_time(capsys):
    startup.log_lifecycle_event(
        "example",
        started_at=10.0,
        monotonic=lambda: 12.345,
    )

    assert capsys.readouterr().out.strip() == (
        "production-startup event=example elapsed_ms=2345"
    )


def test_run_emits_success_lifecycle_events_in_order(monkeypatch, capsys):
    backend = FakeProcess()
    frontend = FakeProcess()

    monkeypatch.setattr(startup, "start_backend", lambda: backend)
    monkeypatch.setattr(startup, "wait_for_backend_ready", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(startup, "start_frontend", lambda: frontend)
    monkeypatch.setattr(startup, "supervise", lambda _backend, _frontend: 0)

    assert startup.run() == 0
    output = capsys.readouterr().out

    assert _lifecycle_events(output) == [
        "wrapper_start",
        "backend_process_started",
        "backend_readiness_succeeded",
        "frontend_process_started",
        "supervision_entered",
    ]
    lifecycle_lines = [
        line for line in output.splitlines() if line.startswith(startup.LOG_PREFIX)
    ]
    assert all(" elapsed_ms=" in line for line in lifecycle_lines)


def test_run_omits_frontend_lifecycle_events_on_readiness_failure(monkeypatch, capsys):
    backend = FakeProcess()

    monkeypatch.setattr(startup, "start_backend", lambda: backend)

    def refuse(*_args, **_kwargs):
        raise startup.StartupError("not ready")

    monkeypatch.setattr(startup, "wait_for_backend_ready", refuse)
    monkeypatch.setattr(
        startup,
        "start_frontend",
        lambda: pytest.fail("frontend must not start before readiness"),
    )

    assert startup.run() == 1
    output = capsys.readouterr().out

    assert _lifecycle_events(output) == [
        "wrapper_start",
        "backend_process_started",
    ]
    assert "backend_readiness_succeeded" not in output
    assert "frontend_process_started" not in output
    assert "supervision_entered" not in output


def test_replit_deployment_uses_readiness_wrapper():
    replit = (Path(__file__).parents[2] / ".replit").read_text(encoding="utf-8")
    assert 'run = ["python", "scripts/start_production.py"]' in replit
    assert "exec npm --prefix frontend run preview" not in replit
