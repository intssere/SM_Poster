import importlib.util
from pathlib import Path
from types import SimpleNamespace

import pytest


SCRIPT_PATH = Path(__file__).resolve().parents[2] / "scripts" / "start_production.py"
SPEC = importlib.util.spec_from_file_location("start_production_under_test", SCRIPT_PATH)
assert SPEC and SPEC.loader
startup = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(startup)


def test_migration_command_is_repository_resolved_and_shell_free():
    assert startup.migration_command() == [
        startup.sys.executable,
        "-m",
        "alembic",
        "upgrade",
        "head",
    ]


def test_run_database_migrations_uses_backend_cwd_and_no_shell_or_secret_args():
    seen = {}

    def runner(command, **kwargs):
        seen["command"] = command
        seen["kwargs"] = kwargs
        return SimpleNamespace(returncode=0)

    startup.run_database_migrations(runner=runner)

    assert seen["command"] == startup.migration_command()
    assert seen["kwargs"] == {
        "cwd": startup.BACKEND_DIR,
        "check": False,
    }
    rendered = repr((seen["command"], seen["kwargs"]))
    assert "DATABASE_URL" not in rendered
    assert "postgresql://" not in rendered
    assert "password" not in rendered.lower()
    assert "shell" not in seen["kwargs"]


def test_nonzero_migration_exit_fails_closed_without_secret_detail():
    def runner(*args, **kwargs):
        return SimpleNamespace(returncode=17)

    with pytest.raises(startup.StartupError, match="database migration failed with status 17") as exc:
        startup.run_database_migrations(runner=runner)

    message = str(exc.value)
    assert "DATABASE_URL" not in message
    assert "postgresql://" not in message


def test_run_orders_migration_backend_health_frontend_supervision(monkeypatch):
    events = []
    backend = object()
    frontend = object()

    monkeypatch.setattr(startup, "run_database_migrations", lambda: events.append("migration"))
    monkeypatch.setattr(
        startup,
        "start_backend",
        lambda: events.append("backend_start") or backend,
    )
    monkeypatch.setattr(
        startup,
        "wait_for_backend_ready",
        lambda process, **kwargs: (
            events.append("backend_ready"),
            process is backend,
        ),
    )
    monkeypatch.setattr(
        startup,
        "start_frontend",
        lambda: events.append("frontend_start") or frontend,
    )
    monkeypatch.setattr(
        startup,
        "supervise",
        lambda b, f: events.append("supervise") or 0,
    )
    monkeypatch.setattr(startup, "terminate_process", lambda process: None)
    monkeypatch.setattr(startup.signal, "getsignal", lambda signum: object())
    monkeypatch.setattr(startup.signal, "signal", lambda signum, handler: None)
    monkeypatch.setattr(startup, "_timeout_from_environment", lambda: 10.0)
    monkeypatch.setattr(
        startup,
        "log_lifecycle_event",
        lambda event, **kwargs: events.append(f"log:{event}"),
    )

    assert startup.run() == 0

    operational = [event for event in events if not event.startswith("log:")]
    assert operational == [
        "migration",
        "backend_start",
        "backend_ready",
        "frontend_start",
        "supervise",
    ]
    assert events.index("log:database_migration_started") < events.index("migration")
    assert events.index("migration") < events.index("log:database_migration_succeeded")
    assert events.index("log:database_migration_succeeded") < events.index("backend_start")


def test_migration_failure_starts_neither_backend_nor_frontend(monkeypatch):
    calls = []

    def fail_migration():
        calls.append("migration")
        raise startup.StartupError("database migration failed with status 2")

    monkeypatch.setattr(startup, "run_database_migrations", fail_migration)
    monkeypatch.setattr(
        startup,
        "start_backend",
        lambda: (_ for _ in ()).throw(AssertionError("backend must not start")),
    )
    monkeypatch.setattr(
        startup,
        "start_frontend",
        lambda: (_ for _ in ()).throw(AssertionError("frontend must not start")),
    )
    monkeypatch.setattr(startup, "terminate_process", lambda process: None)
    monkeypatch.setattr(startup.signal, "getsignal", lambda signum: object())
    monkeypatch.setattr(startup.signal, "signal", lambda signum, handler: None)

    assert startup.run() == 1
    assert calls == ["migration"]


def test_existing_health_gate_still_blocks_frontend(monkeypatch):
    events = []
    backend = object()

    monkeypatch.setattr(startup, "run_database_migrations", lambda: events.append("migration"))
    monkeypatch.setattr(
        startup,
        "start_backend",
        lambda: events.append("backend_start") or backend,
    )

    def fail_health(process, **kwargs):
        assert process is backend
        events.append("health_fail")
        raise startup.StartupError("backend readiness timed out after 10 seconds")

    monkeypatch.setattr(startup, "wait_for_backend_ready", fail_health)
    monkeypatch.setattr(
        startup,
        "start_frontend",
        lambda: (_ for _ in ()).throw(AssertionError("frontend must not start")),
    )
    monkeypatch.setattr(startup, "terminate_process", lambda process: None)
    monkeypatch.setattr(startup.signal, "getsignal", lambda signum: object())
    monkeypatch.setattr(startup.signal, "signal", lambda signum, handler: None)
    monkeypatch.setattr(startup, "_timeout_from_environment", lambda: 10.0)

    assert startup.run() == 1
    assert events == ["migration", "backend_start", "health_fail"]
