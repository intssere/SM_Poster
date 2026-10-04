"""CLI parity from a backend-only WORKDIR, on fenced disposable PostgreSQL."""
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys

import pytest
import sqlalchemy as sa

from app.state_transfer import migrate_closed_state_once as module, migration_cli
from app.state_transfer import one_shot_migration as once, transfer
from tests.test_migration_closed_state_transfer import source
from tests.test_readiness_execution_admission_0032 import _isolated_database, pytestmark
from tests.test_source_capture_diagnostics import POISON


ROOT = Path(__file__).resolve().parents[2]
ARGS = ["--source-env", "SOURCE_PRIVATE", "--target-env", "TARGET_PRIVATE"]

# Subprocesses do not inherit pytest's Python monkeypatches. Install their
# credential/network/storage fences BEFORE importing any application modules.
# Only event labels are recorded, never DSNs, SQL, options, or row values.
FENCE = r'''
import ipaddress
from pathlib import Path
import socket

trace = Path(@TRACE@)
trace.write_text("")

def event(label):
    with trace.open("a") as f:
        f.write(label + "\n")

def local(host):
    if host is None or host == "localhost":
        return True
    if isinstance(host, bytes):
        host = host.decode("ascii")
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return False

resolve, connect, connect_ex = socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex
def guarded_resolve(host, *a, **kw):
    assert local(host), "External DNS forbidden"
    return resolve(host, *a, **kw)
def guarded_connect(sock, address):
    assert not isinstance(address, tuple) or local(address[0]), "External network forbidden"
    return connect(sock, address)
def guarded_connect_ex(sock, address):
    assert not isinstance(address, tuple) or local(address[0]), "External network forbidden"
    return connect_ex(sock, address)
socket.getaddrinfo, socket.socket.connect, socket.socket.connect_ex = (
    guarded_resolve, guarded_connect, guarded_connect_ex)

import replit.object_storage as sdk
def forbidden_storage(*a, **kw):
    raise AssertionError("Live storage forbidden")
sdk.Client = forbidden_storage

import psycopg
from app.state_transfer.select_bridge import source_sql
from app.state_transfer import one_shot_migration
if Path.cwd().name == "app":
    assert one_shot_migration.ROOT == Path.cwd(), "Incorrect backend-only export boundary"
    assert Path(one_shot_migration.__file__).is_relative_to(Path.cwd()), "Repository import forbidden"
expected = source_sql()
class ObservedCursor(psycopg.Cursor):
    def execute(self, query, *a, **kw):
        event("capture" if query == expected else "setup")
        return super().execute(query, *a, **kw)

original = psycopg.connect
def observed_connect(*a, **kw):
    # libpq can bypass Python socket guards; reject non-local DSNs here too.
    info = psycopg.conninfo.conninfo_to_dict(
        a[0] if a else "", **{k: v for k, v in kw.items() if k in (
            "host", "hostaddr", "port", "dbname", "user", "password", "options")})
    host = info.get("host", "")
    assert host.startswith("/") or host in ("localhost", "127.0.0.1", "::1"), "Nonlocal PG forbidden"
    assert not info.get("hostaddr") or local(info["hostaddr"]), "Nonlocal PG forbidden"
    options = kw.get("options", "")
    is_source = "default_transaction_read_only=on" in options
    event("source_connect" if is_source else "target_connect")
    if is_source:
        assert "statement_timeout=480000" in options and "lock_timeout=10000" in options
        kw["cursor_factory"] = ObservedCursor
    return original(*a, **kw)
psycopg.connect = observed_connect
event("fenced")
'''


@pytest.fixture
def entrypoints(tmp_path):
    # Same contents as COPY . . relevant to this module, but no repository root,
    # scripts/, migrations, tests, or sys.path reference to the original backend.
    workdir = tmp_path / "container" / "app"
    shutil.copytree(ROOT / "backend/app", workdir / "app",
                    ignore=shutil.ignore_patterns("__pycache__", "*.pyc"))
    assert not (workdir / "scripts").exists()
    assert not (workdir.parent / "scripts").exists()
    fences, exports = tmp_path / "fences", tmp_path / "exports"
    fences.mkdir()
    exports.mkdir()
    trace = fences / "events"
    (fences / "sitecustomize.py").write_text(FENCE.replace("@TRACE@", repr(str(trace))))

    def run(kind, args, values=None):
        cwd = workdir if kind == "module" else ROOT / "backend"
        cmd = ([sys.executable, "-m", "app.state_transfer.migrate_closed_state_once"]
               if kind == "module" else
               [sys.executable, str(ROOT / "scripts/migrate_closed_state_once.py")])
        # Parent pytest is already credential-cleared. Still use an allowlist,
        # never copy or inspect inherited service credentials.
        env = {k: os.environ[k] for k in ("PATH", "HOME", "USER", "LANG") if k in os.environ}
        env.update({"PYTHONPATH": str(fences) + os.pathsep + str(cwd),
                    "TMPDIR": str(exports), "UNUSED_PRIVATE": POISON})
        env.update(values or {})
        proc = subprocess.run(cmd + args, cwd=cwd, env=env, capture_output=True,
                              text=True, timeout=180, check=False)
        events = trace.read_text().splitlines()
        assert events[0] == "fenced"  # sitecustomize failed silently => test fails.
        assert proc.stderr == ""
        assert POISON not in proc.stdout
        for value in (values or {}).values():
            assert value not in proc.stdout
        assert list(exports.iterdir()) == []
        return proc.returncode, json.loads(proc.stdout), events

    return run


@pytest.mark.parametrize("module_file,expected", [
    ("/app/app/state_transfer/one_shot_migration.py", "/app"),
    ("/repo/backend/app/state_transfer/one_shot_migration.py", "/repo"),
    ("/backend/app/state_transfer/one_shot_migration.py", "/backend"),
])
def test_export_boundary_never_confuses_workdir_with_filesystem_root(module_file, expected):
    assert once._export_boundary(module_file) == Path(expected)


def test_both_entrypoints_use_identical_cli_and_exact_json(monkeypatch, capsys):
    spec = importlib.util.spec_from_file_location(
        "repository_migration_cli", ROOT / "scripts/migrate_closed_state_once.py")
    wrapper = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(wrapper)
    assert wrapper.main is module.main is migration_cli.main
    assert wrapper.SafeParser is migration_cli.SafeParser
    for result, expected_exit in [
        ({"success": True, "status": "PASS"}, 0),
        ({"success": False, "diagnostic": {"stage": "EXECUTION_GATE"}}, 2),
    ]:
        calls = []

        def observed(**kwargs):
            calls.append(kwargs)
            return result

        monkeypatch.setattr(once, "run_migration", observed)
        assert module.main(ARGS) == expected_exit
        first = capsys.readouterr()
        assert wrapper.main(ARGS) == expected_exit
        assert capsys.readouterr() == first
        assert len(calls) == 2 and calls[0] == calls[1]
        assert calls[0]["execute"] is False
        assert calls[0]["statement_timeout_ms"] == 480000
        assert calls[0]["lock_timeout_ms"] == 10000


@pytest.mark.parametrize("args,values,stage", [
    (ARGS, {"SOURCE_PRIVATE": POISON, "TARGET_PRIVATE": POISON}, "EXECUTION_GATE"),
    (ARGS + ["--execution-env", "PRIVATE_ACK"], {"PRIVATE_ACK": POISON}, "EXECUTION_GATE"),
    (ARGS + ["--execute"], {}, "CONFIGURATION"),
    (ARGS + ["--execute"], {"SOURCE_PRIVATE": POISON, "TARGET_PRIVATE": POISON}, "CONFIGURATION"),
    (["--unknown", POISON], {}, "ARGUMENTS"),
    (ARGS + ["--statement-timeout-ms", POISON], {}, "ARGUMENTS"),
    (["--source-env", POISON], {}, "ARGUMENTS"),
])
def test_equivalent_safe_refusals_before_any_connection(entrypoints, args, values, stage):
    left = entrypoints("module", args, values)
    right = entrypoints("repository", args, values)
    assert left == right
    code, result, events = left
    assert code == 2 and result["success"] is False
    assert result["diagnostic"]["stage"] == stage
    assert events == ["fenced"]


def test_backend_only_and_repository_success_parity_single_capture(source, entrypoints):
    results = []
    for kind in ("module", "repository"):
        with _isolated_database("0034") as (target, _):
            values = {
                "SOURCE_PRIVATE": source.url.render_as_string(hide_password=False),
                "TARGET_PRIVATE": target.url.render_as_string(hide_password=False),
            }
            code, result, events = entrypoints(kind, ARGS + ["--execute"], values)
            assert code == 0 and result["success"] is True
            assert result["atomic_certification"] == result["durable_certification"] == "PASS"
            assert result["statement_count"] == 1
            assert events.count("source_connect") == events.count("capture") == 1
            assert events.count("setup") == 1
            assert result["ephemeral_cleanup"] == "PASS"
            with target.connect() as c:
                assert transfer.count(c, "products") == 2997
                assert transfer.count(c, "stores") == 1
            # Independent captures necessarily have different snapshot digests.
            # Require identical safe JSON except these two validated SHA256s.
            for key in ("capsule_sha256", "manifest_sha256"):
                assert re.fullmatch("[a-f0-9]{64}", result.pop(key))
            results.append((code, result))
            again_code, again, again_events = entrypoints(kind, ARGS + ["--execute"], values)
            assert again_code == 2 and again["diagnostic"]["stage"] == "TARGET_PREFLIGHT"
            assert again_events.count("capture") == again_events.count("source_connect") == 1
            with target.connect() as c:
                assert transfer.count(c, "products") == 2997
    assert results[0] == results[1]


def test_real_capture_failure_is_equivalent_and_not_retried(source, entrypoints):
    results = []
    with source.begin() as c:
        c.exec_driver_sql("UPDATE public.alembic_version SET version_num='0030'")
    try:
        with _isolated_database("0034") as (target, _):
            values = {
                "SOURCE_PRIVATE": source.url.render_as_string(hide_password=False),
                "TARGET_PRIVATE": target.url.render_as_string(hide_password=False),
            }
            for kind in ("module", "repository"):
                code, result, events = entrypoints(kind, ARGS + ["--execute"], values)
                assert code == 2 and result["success"] is False
                assert result["diagnostic"]["stage"] == "EXECUTE_SINGLE_STATEMENT"
                assert events.count("source_connect") == events.count("capture") == 1
                assert "target_connect" not in events
                results.append((code, result))
            with target.connect() as c:
                assert transfer.count(c, "stores") == 0
                assert transfer.count(c, "products") == 0
    finally:
        with source.begin() as c:
            c.exec_driver_sql("UPDATE public.alembic_version SET version_num='0031'")
    assert results[0] == results[1]