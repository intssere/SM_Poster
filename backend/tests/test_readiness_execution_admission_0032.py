"""PostgreSQL durability, immutability, and migration checks for revision 0032."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
import hashlib
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
from threading import Barrier, Lock, local
from types import SimpleNamespace
from uuid import uuid4

import pytest
import sqlalchemy as sa
from sqlalchemy.engine import make_url

from app.db.migration_adoption import (
    SchemaAdoptionRefused,
    adopt_managed_preapplied_0031,
    verify_frozen_schema_at_head,
)
from app.db import schema_canonicality_guard
from app.services.readiness_execution_admission import (
    PostgresReadinessAdmission,
    management_readiness_admissions,
)
from app.services import readiness_execution_admission as admission_module
from app.services.readiness_execution_contract import (
    ReadinessBinding,
    ReadinessError,
)


BACKEND = Path(__file__).resolve().parents[1]
DISPOSABLE_POSTGRES_URL = os.getenv("TASK61_DISPOSABLE_POSTGRES_URL")
pytestmark = pytest.mark.skipif(
    not DISPOSABLE_POSTGRES_URL,
    reason="TASK61_DISPOSABLE_POSTGRES_URL local disposable PostgreSQL required",
)


def _socket_postgres_url():
    parsed = make_url(DISPOSABLE_POSTGRES_URL)
    if (
        parsed.get_backend_name() != "postgresql"
        or parsed.password is not None
        or parsed.host is not None
        or not str(parsed.query.get("host", "")).startswith("/")
    ):
        pytest.skip("only password-free local PostgreSQL Unix sockets are allowed")
    return parsed.set(drivername="postgresql+psycopg")


def _safe_env(database_url: str, *, extra: dict[str, str] | None = None) -> dict[str, str]:
    allowed = ("PATH", "HOME", "USER", "LANG")
    environment = {
        key: os.environ[key]
        for key in allowed
        if key in os.environ
    }
    environment.update({
        "PYTHONPATH": str(BACKEND),
        "DATABASE_URL": database_url,
        "APP_ENV": "test",
        "PUBLISHING_ENABLED": "false",
        "BUFFER_PUBLISHING_ENABLED": "false",
        "BUFFER_SINGLE_PIN_PILOT_ENABLED": "false",
        "ROUTINE_PINTEREST_WORKER_ENABLED": "false",
        "ROUTINE_BUFFER_DISPATCH_ENABLED": "false",
        "ROUTINE_SCHEDULED_LIVE_ADMISSION_ENABLED": "false",
        "ROUTINE_PINTEREST_SCHEDULER_ENABLED": "false",
        "ROUTINE_SCHEDULER_CANARY_ENABLED": "false",
        "ROUTINE_SCHEDULED_AUTONOMY_ENABLED": "false",
        "ROUTINE_AUTONOMOUS_AUTHORIZATION_ENABLED": "false",
        "PINTEREST_PORTFOLIO_PLANNER_ENABLED": "false",
        "PINTEREST_SEO_BRIEF_PERSISTENCE_ENABLED": "false",
        "PINTEREST_AUTONOMOUS_GENERATION_ENABLED": "false",
        "PINTEREST_ANALYTICS_INGESTION_ENABLED": "false",
        "PINTEREST_LEARNING_SNAPSHOT_PERSISTENCE_ENABLED": "false",
        "PINTEREST_OPTIMIZER_ENABLED": "false",
        "PINTEREST_PORTFOLIO_ACTIVATION_ENABLED": "false",
        "PINTEREST_OPTIMIZER_APPLY_ENABLED": "false",
        "PINTEREST_AUTONOMOUS_EXECUTION_ENABLED": "false",
        "PINTEREST_AUTONOMOUS_BOARD_ENSURE_ENABLED": "false",
        "PINTEREST_WRITE_SCOPE_ENABLED": "false",
        "PINTEREST_BOARD_WRITE_SCOPE_ENABLED": "false",
        "PINTEREST_BOARD_PROVISIONING_ENABLED": "false",
        "PINTEREST_SINGLE_PIN_PILOT_ENABLED": "false",
    })
    if extra:
        environment.update(extra)
    return environment


def _run_alembic(
    database_url: str,
    revision: str,
    *,
    direction: str = "upgrade",
) -> subprocess.CompletedProcess:
    if direction not in {"upgrade", "downgrade"}:
        raise ValueError("unsupported Alembic direction")
    code = (
        "from alembic import command\n"
        "from alembic.config import Config\n"
        f"cfg = Config({str(BACKEND / 'alembic.ini')!r})\n"
        f"cfg.set_main_option('script_location', {str(BACKEND / 'alembic')!r})\n"
        f"command.{direction}(cfg, {revision!r})\n"
    )
    with tempfile.TemporaryDirectory(prefix="readiness-alembic-") as clean_cwd:
        return subprocess.run(
            [sys.executable, "-c", code],
            cwd=clean_cwd,
            env=_safe_env(database_url),
            check=False,
            capture_output=True,
            text=True,
        )


@contextmanager
def _isolated_database(target_revision: str = "0032"):
    base = _socket_postgres_url()
    admin = sa.create_engine(
        base.set(database="postgres"),
        isolation_level="AUTOCOMMIT",
        pool_pre_ping=True,
    )
    name = f"task61_{uuid4().hex[:16]}"
    try:
        with admin.connect() as connection:
            connection.execute(sa.text(f'CREATE DATABASE "{name}"'))
        database_url = base.set(database=name).render_as_string(hide_password=False)
        upgrade = _run_alembic(database_url, target_revision)
        assert upgrade.returncode == 0, "isolated PostgreSQL migration failed"
        engine = sa.create_engine(database_url, pool_pre_ping=True)
        try:
            yield engine, database_url
        finally:
            engine.dispose()
    finally:
        with admin.connect() as connection:
            connection.execute(sa.text(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE datname=:name AND pid <> pg_backend_pid()"
            ), {"name": name})
            connection.execute(sa.text(f'DROP DATABASE IF EXISTS "{name}"'))
        admin.dispose()


def _binding(release: str = "d") -> ReadinessBinding:
    return ReadinessBinding(
        canonical_commit_sha="a" * 40,
        canonical_tree_sha="b" * 40,
        release_commit_sha=release * 40,
        release_tree_sha="e" * 40,
        overlay_sha256="c" * 64,
        probe_sha256="f" * 64,
        topology="autoscale",
    )


def _runner_receipt(status="PASS", error_code=None):
    from test_readiness_execution_runner import _receipt

    return _receipt(status, error_code)


def _revision(engine):
    with engine.connect() as connection:
        return connection.execute(sa.text(
            'SELECT version_num FROM "public"."alembic_version" ORDER BY version_num'
        )).scalars().all()


def _table_row_counts(engine):
    with engine.connect() as connection:
        tables = connection.execute(sa.text(
            "SELECT tablename FROM pg_tables WHERE schemaname='public' "
            "AND tablename <> 'alembic_version' "
            "AND tablename <> 'management_readiness_admissions' ORDER BY tablename"
        )).scalars().all()
        return {
            table: connection.scalar(
                sa.text(f'SELECT count(*) FROM "public"."{table}"')
            )
            for table in tables
        }


def _management_clients(monkeypatch, tmp_path, database_url, engines, *, barrier_size=0):
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    from app.api.routes import object_storage_readiness as routes
    from app.core import auth, readiness_execution_auth as grants
    from app.core.config import Settings
    from app.middleware import AdminAuthMiddleware
    from app.services import readiness_execution as execution
    from app.services import readiness_execution_runner as runner
    from app.services.deployment_attestation import BuildProvenance

    origin = "http://localhost:5000"
    overlay = tmp_path / ".replit"
    probe = tmp_path / "probe.py"
    overlay.write_bytes(b"synthetic readiness overlay")
    probe.write_bytes(b"synthetic fixed probe")
    settings = Settings(
        _env_file=None,
        database_url=database_url,
        app_env="test",
        app_secret_key="synthetic-readiness-management-session-key-" * 2,
        admin_username="synthetic-admin",
        admin_password_hash="synthetic-admin-password-hash",
        auth_allowed_origins=origin,
        object_storage_readiness_management_enabled="true",
        readiness_expected_canonical_commit_sha="a" * 40,
        readiness_expected_canonical_tree_sha="b" * 40,
        readiness_expected_release_commit_sha="c" * 40,
        readiness_expected_release_tree_sha="d" * 40,
        readiness_expected_overlay_sha256=hashlib.sha256(
            overlay.read_bytes()
        ).hexdigest(),
        readiness_expected_probe_sha256=hashlib.sha256(
            probe.read_bytes()
        ).hexdigest(),
        readiness_expected_topology="canonical_parent_with_checkpoint_overlay",
    )
    monkeypatch.setattr(auth, "get_settings", lambda: settings)
    monkeypatch.setattr(grants, "get_settings", lambda: settings)
    monkeypatch.setattr(routes, "get_settings", lambda: settings)
    from app import middleware
    monkeypatch.setattr(middleware, "get_settings", lambda: settings)
    monkeypatch.setenv("REPLIT_DEPLOYMENT", "1")
    monkeypatch.setenv(execution.PARENT_GATE, "false")
    monkeypatch.setattr(execution, "OVERLAY_PATH", overlay)
    monkeypatch.setattr(execution, "PROBE_PATH", probe)
    binding = grants.binding_from_settings(settings)
    provenance = BuildProvenance(
        present=True,
        valid=True,
        commit_sha=binding.canonical_commit_sha,
        tree_sha=binding.canonical_tree_sha,
        release_commit_sha=binding.release_commit_sha,
        release_tree_sha=binding.release_tree_sha,
        topology=binding.topology,
        overlay_path=".replit",
        overlay_sha256=binding.overlay_sha256,
    )
    monkeypatch.setattr(execution, "read_build_provenance", lambda: provenance)
    monkeypatch.setattr(execution, "require_scheduler_stopped", lambda _settings: None)

    engine_lock = Lock()
    engine_iterator = iter(engines)

    def next_engine():
        with engine_lock:
            return next(engine_iterator)

    monkeypatch.setattr(execution, "execution_engine", next_engine)
    if barrier_size:
        sync_barrier = Barrier(barrier_size)
        thread_state = local()
        real_snapshot = execution.business_snapshot

        def synchronize_first_snapshots(engine):
            snapshot = real_snapshot(engine)
            if not getattr(thread_state, "baseline_reached", False):
                thread_state.baseline_reached = True
                sync_barrier.wait(timeout=20)
            return snapshot

        monkeypatch.setattr(execution, "business_snapshot", synchronize_first_snapshots)

    app_clients = []
    for _engine in engines:
        app = FastAPI()
        app.add_middleware(AdminAuthMiddleware)
        app.include_router(routes.router, prefix="/api")
        client = TestClient(app)
        client.cookies.set(
            auth.SESSION_COOKIE,
            auth.make_session(settings.admin_username),
        )
        app_clients.append(client)

    headers = {
        "Origin": origin,
        grants.CONFIRM_HEADER: grants.CONFIRMATION,
        grants.GRANT_HEADER: grants.issue_authorization(settings, binding),
    }
    return SimpleNamespace(
        clients=app_clients,
        headers=headers,
        settings=settings,
        binding=binding,
        routes=routes,
        execution=execution,
        runner=runner,
        engines=engines,
    )


@pytest.fixture
def database():
    with _isolated_database("0032") as result:
        yield result


def test_clean_full_upgrade_reaches_frozen_0032_and_verifies():
    with _isolated_database("head") as (engine, _url):
        assert _revision(engine) == ["0032"]
        with engine.connect() as connection:
            verify_frozen_schema_at_head(connection, revision="0032")
        original_engine = schema_canonicality_guard.engine
        try:
            schema_canonicality_guard.engine = engine
            assert schema_canonicality_guard.main() == 0
        finally:
            schema_canonicality_guard.engine = original_engine
        assert engine.dialect.name == "postgresql"


def test_changed_descriptor_never_hides_consumed_release_scope(database):
    engine, _url = database
    binding = _binding()
    store = PostgresReadinessAdmission(engine)
    assert store.consume(binding, "changed-descriptor-test", "a" * 64)
    changed = replace(binding, overlay_sha256="1" * 64)
    assert store.consume(changed, "another-grant", "a" * 64) is False
    with pytest.raises(ReadinessError, match="ADMISSION_BINDING_MISMATCH"):
        store.lookup(changed)


@pytest.mark.parametrize("exit_code", [None, 1, -15])
def test_unknown_without_receipt_persists_sql_null(database, exit_code):
    engine, _url = database
    binding = _binding()
    store = PostgresReadinessAdmission(engine)
    assert store.consume(binding, "unknown-null-test", "a" * 64)
    store.finish(binding, "UNKNOWN", exit_code, None)
    assert store.lookup(binding) == {
        "admission_state": "CONSUMED", "outcome": "UNKNOWN",
        "exit_code": exit_code, "receipt": None,
    }
    with engine.connect() as connection:
        assert connection.scalar(sa.text(
            "SELECT receipt IS NULL FROM public.management_readiness_admissions"
        )) is True
        assert connection.scalar(sa.text(
            "SELECT outcome FROM public.management_readiness_admissions"
        )) == "UNKNOWN"


def test_0031_to_0032_is_additive_and_mutates_no_business_rows():
    with _isolated_database("0031") as (engine, database_url):
        before = _table_row_counts(engine)
        result = _run_alembic(database_url, "0032")
        assert result.returncode == 0, "0031-to-0032 migration failed"
        assert _revision(engine) == ["0032"]
        assert _table_row_counts(engine) == before
        with engine.connect() as connection:
            assert connection.scalar(sa.text(
                "SELECT count(*) FROM public.management_readiness_admissions"
            )) == 0
            verify_frozen_schema_at_head(connection, revision="0032")


def test_preexisting_0032_target_is_refused_without_stamping():
    with _isolated_database("0031") as (engine, database_url):
        with engine.begin() as connection:
            connection.execute(sa.text(
                "CREATE TABLE public.management_readiness_admissions (unexpected integer)"
            ))
        result = _run_alembic(database_url, "0032")
        assert result.returncode != 0
        assert _revision(engine) == ["0031"]
        with engine.connect() as connection:
            assert connection.scalar(sa.text(
                "SELECT count(*) FROM information_schema.columns "
                "WHERE table_schema='public' "
                "AND table_name='management_readiness_admissions'"
            )) == 1


def test_managed_adoption_does_not_advance_0031_and_verifies_0032():
    with _isolated_database("0031") as (engine, _url):
        with engine.begin() as connection:
            assert adopt_managed_preapplied_0031(connection) is False
        assert _revision(engine) == ["0031"]

    with _isolated_database("0032") as (engine, _url):
        admission = PostgresReadinessAdmission(engine)
        binding = _binding()
        assert admission.consume(binding, "grant-existing", "1" * 64)
        with engine.begin() as connection:
            assert adopt_managed_preapplied_0031(connection) is False
        assert _revision(engine) == ["0032"]


def test_consume_commits_once_and_restart_returns_only_persisted_status():
    with _isolated_database("0032") as (engine, _url):
        binding = _binding()
        first = PostgresReadinessAdmission(engine)
        def interrupted_execution():
            assert first.consume(binding, "grant-restart", "1" * 64) is True
            raise RuntimeError("simulated crash before process spawn")

        with pytest.raises(RuntimeError, match="simulated crash"):
            interrupted_execution()
        # Restart after durable consumption but before process spawn.
        restarted = PostgresReadinessAdmission(engine)
        assert restarted.lookup(binding) == {
            "admission_state": "CONSUMED",
            "outcome": "UNKNOWN",
            "exit_code": None,
            "receipt": None,
        }
        assert restarted.consume(binding, "grant-restart", "1" * 64) is False


def test_expired_grant_checked_against_postgres_clock_after_schema_gate(monkeypatch):
    with _isolated_database("0032") as (engine, _url):
        admission = PostgresReadinessAdmission(engine)
        verified = []
        verify_schema = admission_module.verify_frozen_schema_at_head

        def record_schema_verification(connection, *, revision):
            verified.append(revision)
            return verify_schema(connection, revision=revision)

        monkeypatch.setattr(
            admission_module,
            "verify_frozen_schema_at_head",
            record_schema_verification,
        )
        with pytest.raises(ReadinessError) as failure:
            admission.consume(
                _binding(),
                "grant-expired-at-consume",
                "1" * 64,
                expires_at=1,
            )
        assert verified == ["0032"]
        assert failure.value.code == "EXECUTION_AUTHORIZATION_EXPIRED"
        assert failure.value.status_code == 403
        assert admission.lookup(_binding()) is None
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                management_readiness_admissions
            )) == 0


def test_ambiguous_commit_refuses_execution_and_never_retries(monkeypatch):
    with _isolated_database("0032") as (engine, _url):
        binding = _binding()

        class CommitThenDisconnect:
            def __init__(self, transaction):
                self.transaction = transaction

            def __enter__(self):
                return self.transaction.__enter__()

            def __exit__(self, exc_type, exc, traceback):
                result = self.transaction.__exit__(exc_type, exc, traceback)
                if exc_type is None:
                    raise OSError("simulated lost commit acknowledgement")
                return result

        class ConnectionProxy:
            def __init__(self, connection):
                self.connection = connection

            def __getattr__(self, name):
                return getattr(self.connection, name)

            def begin(self):
                return CommitThenDisconnect(self.connection.begin())

        class AmbiguousEngine:
            def connect(self):
                @contextmanager
                def connected():
                    with engine.connect() as connection:
                        yield ConnectionProxy(connection)
                return connected()

        verify_schema = admission_module.verify_frozen_schema_at_head

        def verify_underlying_connection(connection, *, revision):
            if isinstance(connection, ConnectionProxy):
                connection = connection.connection
            return verify_schema(connection, revision=revision)

        monkeypatch.setattr(
            admission_module,
            "verify_frozen_schema_at_head",
            verify_underlying_connection,
        )
        admission = PostgresReadinessAdmission(AmbiguousEngine())
        with pytest.raises(ReadinessError) as failure:
            admission.consume(binding, "grant-ambiguous", "2" * 64)
        assert failure.value.code == "COORDINATOR_UNAVAILABLE"
        normal = PostgresReadinessAdmission(engine)
        assert normal.lookup(binding)["outcome"] == "UNKNOWN"
        assert normal.consume(binding, "grant-ambiguous", "2" * 64) is False
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                management_readiness_admissions
            )) == 1


def test_scope_and_grant_uniqueness_refuse_alternate_consumption():
    with _isolated_database("0032") as (engine, _url):
        admission = PostgresReadinessAdmission(engine)
        binding = _binding()
        assert admission.consume(binding, "grant-original", "1" * 64)
        # Same release scope cannot be rebound to another grant.
        assert not admission.consume(binding, "grant-alternate", "2" * 64)
        # A grant cannot be replayed against an alternate release scope.
        assert not admission.consume(_binding("9"), "grant-original", "1" * 64)


def test_racing_engines_and_processes_have_one_winner():
    with _isolated_database("0032") as (engine, database_url):
        binding = _binding()

        def consume_from_engine(_):
            separate = sa.create_engine(database_url, pool_pre_ping=True)
            try:
                return PostgresReadinessAdmission(separate).consume(
                    binding, "grant-race-engines", "3" * 64
                )
            finally:
                separate.dispose()

        with ThreadPoolExecutor(max_workers=6) as pool:
            results = list(pool.map(consume_from_engine, range(6)))
        assert sum(results) == 1

        process_binding = _binding("8")
        process_env = _safe_env(
            database_url,
            extra={
                "TASK61_BINDING": json.dumps(process_binding.__dict__),
                "TASK61_GRANT_ID": "grant-race-processes",
                "TASK61_ACTOR_HASH": "4" * 64,
            },
        )
        child_code = (
            "import json, os\n"
            "import sqlalchemy as sa\n"
            "from app.services.readiness_execution_admission import PostgresReadinessAdmission\n"
            "from app.services.readiness_execution_contract import ReadinessBinding\n"
            "binding = ReadinessBinding(**json.loads(os.environ['TASK61_BINDING']))\n"
            "engine = sa.create_engine(os.environ['DATABASE_URL'], pool_pre_ping=True)\n"
            "try:\n"
            "    result = PostgresReadinessAdmission(engine).consume(\n"
            "        binding, os.environ['TASK61_GRANT_ID'], os.environ['TASK61_ACTOR_HASH'])\n"
            "    print('WIN' if result else 'DUPLICATE')\n"
            "finally:\n"
            "    engine.dispose()\n"
        )
        with tempfile.TemporaryDirectory(prefix="readiness-race-") as clean_cwd:
            processes = [
                subprocess.Popen(
                    [sys.executable, "-c", child_code],
                    cwd=clean_cwd,
                    env=process_env,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for _ in range(3)
            ]
            outputs = [process.communicate(timeout=30) for process in processes]
        assert all(process.returncode == 0 for process in processes)
        assert sum(stdout.strip() == "WIN" for stdout, _stderr in outputs) == 1
        assert sum(stdout.strip() == "DUPLICATE" for stdout, _stderr in outputs) == 2
        assert engine.dialect.name == "postgresql"


def test_independent_http_instances_race_to_exactly_one_fake_launch(
    database, tmp_path, monkeypatch
):
    engine_a, database_url = database
    engine_b = sa.create_engine(database_url, pool_pre_ping=True)
    try:
        management = _management_clients(
            monkeypatch,
            tmp_path,
            database_url,
            [engine_a, engine_b],
            barrier_size=2,
        )
        launch_lock = Lock()
        launches = []

        def fake_launch_probe():
            with launch_lock:
                launches.append("fake-child")
            return {
                "outcome": "PASS",
                "exit_code": 0,
                "receipt": _runner_receipt(),
            }

        monkeypatch.setattr(
            management.runner, "launch_probe", fake_launch_probe
        )
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = [
                pool.submit(
                    client.post,
                    "/api/internal/operations/object-storage-readiness",
                    headers=management.headers,
                )
                for client in management.clients
            ]
            responses = [future.result(timeout=30) for future in responses]
        assert sorted(response.status_code for response in responses) == [200, 409]
        refusal = next(response for response in responses if response.status_code == 409)
        assert refusal.json()["code"] == "ADMISSION_ALREADY_CONSUMED"
        assert launches == ["fake-child"]
        with engine_a.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                management_readiness_admissions
            )) == 1
    finally:
        engine_b.dispose()


def test_lost_http_response_then_restart_replay_is_409_without_second_launch(
    database, tmp_path, monkeypatch
):
    engine_a, database_url = database
    engine_b = sa.create_engine(database_url, pool_pre_ping=True)
    try:
        management = _management_clients(
            monkeypatch, tmp_path, database_url, [engine_a, engine_b]
        )
        launches = []

        def fake_launch_probe():
            launches.append("fake-child")
            return {
                "outcome": "PASS",
                "exit_code": 0,
                "receipt": _runner_receipt(),
            }

        monkeypatch.setattr(
            management.runner, "launch_probe", fake_launch_probe
        )
        real_execute = management.routes.execute_readiness

        def execute_then_lose_response(settings, binding, claims):
            real_execute(settings, binding, claims)
            raise RuntimeError("synthetic response lost after committed outcome")

        monkeypatch.setattr(
            management.routes, "execute_readiness", execute_then_lose_response
        )
        with pytest.raises(RuntimeError, match="synthetic response lost"):
            management.clients[0].post(
                "/api/internal/operations/object-storage-readiness",
                headers=management.headers,
            )

        # A restarted application/engine receives the same still-valid grant;
        # durable consumption blocks a second child despite the lost response.
        monkeypatch.setattr(management.routes, "execute_readiness", real_execute)
        replay = management.clients[1].post(
            "/api/internal/operations/object-storage-readiness",
            headers=management.headers,
        )
        assert replay.status_code == 409
        assert replay.json()["code"] == "ADMISSION_ALREADY_CONSUMED"
        assert launches == ["fake-child"]
        persisted = PostgresReadinessAdmission(engine_b).lookup(management.binding)
        assert persisted == {
            "admission_state": "CONSUMED",
            "outcome": "PASS",
            "exit_code": 0,
            "receipt": _runner_receipt(),
        }
    finally:
        engine_b.dispose()


def test_finish_is_terminal_idempotent_and_lookup_maps_admitted_to_unknown():
    with _isolated_database("0032") as (engine, _url):
        admission = PostgresReadinessAdmission(engine)
        binding = _binding()
        assert admission.consume(binding, "grant-finish", "5" * 64)
        receipt = _runner_receipt()
        admission.finish(binding, "PASS", 0, receipt)
        assert admission.lookup(binding) == {
            "admission_state": "CONSUMED",
            "outcome": "PASS",
            "exit_code": 0,
            "receipt": receipt,
        }
        admission.finish(binding, "PASS", 0, receipt)
        with pytest.raises(ReadinessError) as failure:
            admission.finish(binding, "FAILED", 1, None)
        assert failure.value.code == "OUTCOME_PERSISTENCE_FAILED"


def test_finish_rejects_invalid_outcome_evidence_and_accepts_unknown_pass_receipt():
    with _isolated_database("0032") as (engine, _url):
        admission = PostgresReadinessAdmission(engine)
        binding = _binding()
        assert admission.consume(binding, "grant-finish-invalid", "5" * 64)
        passed = _runner_receipt()
        failed = _runner_receipt("FAILED", "SDK_OPERATION_FAILED")
        for outcome, exit_code, receipt in (
            ("PASS", 0, None),
            ("PASS", 1, passed),
            ("PASS", 0, failed),
            ("FAILED", 1, None),
            ("FAILED", 0, failed),
            ("FAILED", 1, passed),
            ("UNKNOWN", 1, passed),
            ("UNKNOWN", 0, {"invalid": "receipt"}),
        ):
            with pytest.raises(ReadinessError) as failure:
                admission.finish(binding, outcome, exit_code, receipt)
            assert failure.value.code == "OUTCOME_PERSISTENCE_FAILED"
        assert admission.lookup(binding) == {
            "admission_state": "CONSUMED",
            "outcome": "UNKNOWN",
            "exit_code": None,
            "receipt": None,
        }
        # A post-probe safety failure may retain a valid PASS receipt as UNKNOWN.
        admission.finish(binding, "UNKNOWN", 0, passed)
        assert admission.lookup(binding) == {
            "admission_state": "CONSUMED",
            "outcome": "UNKNOWN",
            "exit_code": 0,
            "receipt": passed,
        }


@pytest.mark.parametrize("status,exit_code,error_code", [
    ("FAILED", 1, "SDK_OPERATION_FAILED"),
    ("BLOCKED", 2, "GATE_DISABLED"),
])
def test_finish_persists_only_matching_failed_or_blocked_receipt(
    status, exit_code, error_code
):
    with _isolated_database("0032") as (engine, _url):
        admission = PostgresReadinessAdmission(engine)
        binding = _binding()
        assert admission.consume(binding, "grant-real-failure", "7" * 64)
        receipt = _runner_receipt(status, error_code)
        admission.finish(binding, "FAILED", exit_code, receipt)
        assert admission.lookup(binding) == {
            "admission_state": "CONSUMED",
            "outcome": "FAILED",
            "exit_code": exit_code,
            "receipt": receipt,
        }


@pytest.mark.parametrize("outcome,exit_code,receipt", [
    ("PASS", 0, None),
    ("PASS", 1, {"final_status": "PASS"}),
    ("PASS", 0, {"final_status": "FAILED"}),
    ("FAILED", 1, None),
    ("FAILED", 2, {"final_status": "FAILED"}),
    ("FAILED", 1, {"final_status": "BLOCKED"}),
])
def test_database_guard_rejects_terminal_outcome_receipt_mismatch(
    outcome, exit_code, receipt
):
    with _isolated_database("0032") as (engine, _url):
        admission = PostgresReadinessAdmission(engine)
        binding = _binding()
        assert admission.consume(binding, "grant-invalid-db-evidence", "6" * 64)
        with pytest.raises(sa.exc.DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    management_readiness_admissions.update()
                    .where(
                        management_readiness_admissions.c.release_commit_sha
                        == binding.release_commit_sha
                    )
                    .values(
                        outcome=outcome,
                        exit_code=exit_code,
                        receipt=receipt,
                        finished_at=sa.func.now(),
                    )
                )
        assert admission.lookup(binding)["outcome"] == "UNKNOWN"


def test_trigger_blocks_binding_rewrite_terminal_rearm_delete_and_truncate():
    with _isolated_database("0032") as (engine, _url):
        admission = PostgresReadinessAdmission(engine)
        binding = _binding()
        assert admission.consume(binding, "grant-immutable", "6" * 64)
        table = management_readiness_admissions
        scope = sa.and_(
            table.c.operation == binding.scope[0],
            table.c.release_commit_sha == binding.scope[1],
            table.c.release_tree_sha == binding.scope[2],
        )
        with pytest.raises(sa.exc.DBAPIError):
            with engine.begin() as connection:
                connection.execute(
                    table.update().where(scope).values(actor_hash="7" * 64)
                )

        admission.finish(binding, "PASS", 0, _runner_receipt())
        for statement in (
            sa.delete(table).where(scope),
            table.update().where(scope).values(outcome="ADMITTED"),
            table.update().where(scope).values(outcome="FAILED", exit_code=1),
        ):
            with pytest.raises(sa.exc.DBAPIError):
                with engine.begin() as connection:
                    connection.execute(statement)
        with pytest.raises(sa.exc.DBAPIError):
            with engine.begin() as connection:
                connection.execute(sa.text(
                    "TRUNCATE TABLE public.management_readiness_admissions"
                ))
        assert admission.lookup(binding)["outcome"] == "PASS"


def test_downgrade_refuses_any_admission_evidence():
    with _isolated_database("0032") as (engine, database_url):
        assert PostgresReadinessAdmission(engine).consume(
            _binding(), "grant-no-downgrade", "8" * 64
        )
        result = _run_alembic(database_url, "0031", direction="downgrade")
        assert result.returncode != 0
        assert _revision(engine) == ["0032"]
        with engine.connect() as connection:
            assert connection.scalar(sa.text(
                "SELECT count(*) FROM public.management_readiness_admissions"
            )) == 1


def test_empty_database_can_downgrade_and_restore_0032():
    with _isolated_database("0032") as (engine, database_url):
        result = _run_alembic(database_url, "0031", direction="downgrade")
        assert result.returncode == 0, "empty-evidence downgrade failed"
        assert _revision(engine) == ["0031"]
        with engine.connect() as connection:
            assert connection.scalar(sa.text(
                "SELECT to_regclass('public.management_readiness_admissions')"
            )) is None
        restore = _run_alembic(database_url, "0032", direction="upgrade")
        assert restore.returncode == 0, "0032 restoration failed"
        assert _revision(engine) == ["0032"]
        with engine.connect() as connection:
            verify_frozen_schema_at_head(connection, revision="0032")


def test_admission_is_postgresql_only_and_coordinator_errors_fail_closed():
    sqlite_engine = sa.create_engine("sqlite://")
    try:
        with pytest.raises(ReadinessError) as failure:
            PostgresReadinessAdmission(sqlite_engine).consume(
                _binding(), "grant-no-sqlite", "9" * 64
            )
        assert failure.value.code == "COORDINATOR_UNAVAILABLE"
    finally:
        sqlite_engine.dispose()

    class BrokenEngine:
        def connect(self):
            raise OSError("not surfaced")

    with pytest.raises(ReadinessError) as failure:
        PostgresReadinessAdmission(BrokenEngine()).consume(
            _binding(), "grant-unavailable", "9" * 64
        )
    assert failure.value.code == "COORDINATOR_UNAVAILABLE"


def test_catalog_drift_blocks_admission_before_any_row_is_written():
    with _isolated_database("0032") as (engine, _url):
        with engine.begin() as connection:
            connection.execute(sa.text(
                "ALTER TABLE public.management_readiness_admissions "
                "ALTER COLUMN actor_hash TYPE varchar(63)"
            ))
        with pytest.raises(ReadinessError) as failure:
            PostgresReadinessAdmission(engine).consume(
                _binding(), "grant-drift", "a" * 64
            )
        assert failure.value.code == "COORDINATOR_UNAVAILABLE"
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                management_readiness_admissions
            )) == 0


@pytest.mark.parametrize("ddl", [
    "ALTER TABLE public.management_readiness_admissions "
    "DISABLE TRIGGER management_readiness_admissions_immutable",
    "ALTER TABLE public.management_readiness_admissions "
    "DROP CONSTRAINT ck_management_readiness_admissions_outcome",
])
def test_disabled_trigger_or_missing_constraint_fails_frozen_guard(ddl):
    with _isolated_database("0032") as (engine, _url):
        with engine.begin() as connection:
            connection.execute(sa.text(ddl))
        with engine.connect() as connection:
            with pytest.raises(SchemaAdoptionRefused):
                verify_frozen_schema_at_head(connection, revision="0032")
        with pytest.raises(ReadinessError) as failure:
            PostgresReadinessAdmission(engine).consume(
                _binding(), "grant-schema-drift", "b" * 64
            )
        assert failure.value.code == "COORDINATOR_UNAVAILABLE"
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                management_readiness_admissions
            )) == 0


def test_rewritten_trigger_function_fails_frozen_guard_and_admission():
    with _isolated_database("0032") as (engine, _url):
        with engine.begin() as connection:
            connection.execute(sa.text(
                """
                CREATE OR REPLACE FUNCTION
                    public.management_readiness_admission_guard()
                RETURNS trigger
                LANGUAGE plpgsql
                AS $function$
                BEGIN
                    RETURN NEW;
                END;
                $function$
                """
            ))
        with engine.connect() as connection:
            with pytest.raises(SchemaAdoptionRefused):
                verify_frozen_schema_at_head(connection, revision="0032")
        with pytest.raises(ReadinessError) as failure:
            PostgresReadinessAdmission(engine).consume(
                _binding(), "grant-function-drift", "c" * 64
            )
        assert failure.value.code == "COORDINATOR_UNAVAILABLE"
        with engine.connect() as connection:
            assert connection.scalar(sa.select(sa.func.count()).select_from(
                management_readiness_admissions
            )) == 0
