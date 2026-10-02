"""Only disposable local PostgreSQL; no attached DB or platform adapter."""
from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import time
from threading import Barrier

from alembic import command
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
import pytest
import sqlalchemy as sa

from app.db import exact_revision_runner as runner
from app.db.database_identity import database_identity_sha256, connected_database_identity_sha256
from app.db.migration_lock import (
    MIGRATION_ADVISORY_LOCK_KEY, acquire_transaction_lock, clear_lock_proof,
)
from test_readiness_execution_admission_0032 import (
    DISPOSABLE_POSTGRES_URL, _isolated_database, _run_alembic, _safe_env, _revision,
)

pytestmark = pytest.mark.skipif(
    not DISPOSABLE_POSTGRES_URL,
    reason="TASK61_DISPOSABLE_POSTGRES_URL local disposable PostgreSQL required",
)

BACKEND = Path(__file__).resolve().parents[1]
KEY = Ed25519PrivateKey.generate()
PUBLIC_KEY = KEY.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw)


@pytest.fixture
def database():
    # The existing helper accepts only password-free disposable Unix sockets.
    with _isolated_database("0031") as (original_engine, url):
        # Keep connection routing on the Unix socket. The identity helper's
        # endpoint host is synthetic and must never be resolved or contacted.
        local_url = original_engine.url.set(host="fixture.invalid")
        engine = sa.create_engine(local_url, pool_pre_ping=True)
        try:
            yield engine, url
        finally:
            engine.dispose()


def authorization(engine, **overrides):
    identity = database_identity_sha256(engine.url.render_as_string(hide_password=False))
    with engine.connect() as c:
        server_identity = connected_database_identity_sha256(c)
    policy = runner.InvocationPolicy(
        PUBLIC_KEY, "fixture-app", "a" * 64, identity, "b" * 64, "fixture-job",
        server_identity, "d" * 64,
    )
    claims = {
        "operation": "alembic_0031_to_0032", "scope": "production",
        "invocation": "platform_migration_job", "app_id": policy.app_id,
        "artifact_sha256": policy.artifact_sha256,
        "database_identity": policy.production_identity, "job_id": policy.job_id,
        "server_identity": policy.production_server_identity,
        "issued_at": time.time() - 1, "expires_at": time.time() + 240,
    }
    claims.update(overrides)
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    closed = {k: "false" for k in runner.DISABLED_GATES}
    closed.update(APP_ENV="production", AI_PROVIDER="none", ROUTINE_PINTEREST_DRY_RUN="true")
    return dict(payload=payload, signature=KEY.sign(payload), policy=policy, closed_state=closed)


def run(engine, **kwargs):
    with engine.connect() as connection:
        transaction = connection.begin()
        try:
            return runner.run_exact_0032(connection, transaction, **(kwargs or authorization(engine)))
        finally:
            if transaction.is_active:
                transaction.rollback()


def evidence(engine):
    """Stable schemas and server-side content hashes, never business-row logs."""
    with engine.connect() as c:
        inspector = sa.inspect(c)
        result = {}
        quote = c.dialect.identifier_preparer.quote_identifier
        for name in sorted(inspector.get_table_names(schema="public")):
            if name in {"alembic_version", "management_readiness_admissions"}:
                continue
            contract = (
                repr(inspector.get_columns(name, schema="public")),
                repr(inspector.get_pk_constraint(name, schema="public")),
                repr(inspector.get_unique_constraints(name, schema="public")),
                repr(inspector.get_check_constraints(name, schema="public")),
                repr(inspector.get_foreign_keys(name, schema="public")),
                repr(inspector.get_indexes(name, schema="public")),
            )
            digest = c.execute(sa.text(
                "SELECT count(*),md5(COALESCE(string_agg(h,'' ORDER BY h),'')) "
                "FROM (SELECT md5(to_jsonb(t)::text) h FROM public."
                + quote(name) + " t) hashes"
            )).one()
            result[name] = (contract, tuple(digest))
        return result


def assert_0031_untouched(engine):
    assert _revision(engine) == ["0031"]
    with engine.connect() as c:
        runner.verify_frozen_schema_at_head(c, revision="0031")
        assert c.scalar(sa.text(
            "SELECT to_regclass('public.management_readiness_admissions')"
        )) is None
        assert c.scalar(sa.text(
            "SELECT count(*) FROM pg_proc p JOIN pg_namespace n ON n.oid=p.pronamespace "
            "WHERE n.nspname='public' AND p.proname=ANY(:names)"
        ), {"names": list(runner.FUNCTIONS)}) == 0


def test_success_and_certified_noop_preserve_business_data(database):
    engine, _ = database
    with engine.begin() as c:
        c.exec_driver_sql("CREATE TABLE public.fixture_business (id integer PRIMARY KEY, value text NOT NULL)")
        c.exec_driver_sql("INSERT INTO public.fixture_business VALUES (1,'preserve'),(2,'same')")
    before = evidence(engine)
    assert run(engine).status == "migrated"
    assert _revision(engine) == ["0032"]
    assert evidence(engine) == before
    assert run(engine).status == "verified_noop"
    assert evidence(engine) == before
    with engine.connect() as c:
        runner.verify_frozen_schema_at_head(c, revision="0032")
        assert c.scalar(sa.text("SELECT count(*) FROM public.management_readiness_admissions")) == 0
        assert [tuple(r) for r in c.execute(sa.text(
            "SELECT id,state FROM public.routine_publishing_control ORDER BY id"
        ))] == [("default", "PAUSED")]


def test_concurrent_calls_one_migration_one_noop(database):
    engine, _ = database
    barrier = Barrier(2)
    def invoke():
        barrier.wait()
        return run(engine).status
    with ThreadPoolExecutor(max_workers=2) as pool:
        results = list(pool.map(lambda _: invoke(), range(2)))
    assert sorted(results) == ["migrated", "verified_noop"]


def test_bounded_contention_with_normal_cli_lock(database):
    engine, _ = database
    args = authorization(engine)
    args["lock_timeout"] = 0.15
    with engine.connect() as holder:
        holder.execute(sa.text("SELECT pg_advisory_lock(:key)"), {"key": MIGRATION_ADVISORY_LOCK_KEY})
        try:
            started = time.monotonic()
            with pytest.raises(runner.MigrationRefused):
                run(engine, **args)
            assert time.monotonic() - started < 2
        finally:
            holder.execute(sa.text("SELECT pg_advisory_unlock(:key)"), {"key": MIGRATION_ADVISORY_LOCK_KEY})
    assert_0031_untouched(engine)


def test_ambiguous_preowned_lock_refused(database):
    engine, _ = database
    with engine.connect() as c:
        tx = c.begin()
        c.execute(sa.text("SELECT pg_advisory_xact_lock(:key)"), {"key": MIGRATION_ADVISORY_LOCK_KEY})
        with pytest.raises(runner.MigrationRefused):
            runner.run_exact_0032(c, tx, **authorization(engine))
        assert not tx.is_active
    assert_0031_untouched(engine)


@pytest.mark.parametrize("fault", ["ddl", "certification"])
def test_fault_rolls_back_ddl_and_version(database, monkeypatch, fault):
    engine, _ = database
    before = evidence(engine)
    if fault == "ddl":
        from sqlalchemy import event
        def fail(conn, cursor, statement, parameters, context, many):
            if "CREATE TRIGGER management_readiness_admissions_no_truncate" in statement:
                raise RuntimeError("injected DDL boundary")
        event.listen(engine, "before_cursor_execute", fail)
    else:
        original = runner.verify_frozen_schema_at_head
        def fail(c, revision):
            if revision == "0032":
                assert c.scalar(sa.text("SELECT count(*) FROM public.management_readiness_admissions")) == 0
                assert c.scalar(sa.text("SELECT version_num FROM public.alembic_version")) == "0032"
                raise RuntimeError("injected before commit")
            return original(c, revision=revision)
        monkeypatch.setattr(runner, "verify_frozen_schema_at_head", fail)
    try:
        with pytest.raises(runner.MigrationRefused):
            run(engine)
    finally:
        if fault == "ddl":
            event.remove(engine, "before_cursor_execute", fail)
        else:
            monkeypatch.setattr(runner, "verify_frozen_schema_at_head", original)
    assert_0031_untouched(engine)
    assert evidence(engine) == before


@pytest.mark.parametrize("partial", ["table", "function", "index_name", "overload"])
def test_partial_objects_refused(database, partial):
    engine, _ = database
    ddl = {
        "table": "CREATE TABLE public.management_readiness_admissions (id integer)",
        "function": "CREATE FUNCTION public.management_readiness_admission_guard() RETURNS integer LANGUAGE sql AS 'SELECT 1'",
        "overload": "CREATE FUNCTION public.management_readiness_admission_guard(integer) RETURNS integer LANGUAGE sql AS 'SELECT 1'",
        "index_name": "CREATE TABLE public.pk_management_readiness_admissions (id integer)",
    }[partial]
    with engine.begin() as c:
        c.exec_driver_sql(ddl)
    with pytest.raises(runner.MigrationRefused, match="pre-existing"):
        run(engine)
    assert _revision(engine) == ["0031"]
    with engine.connect() as c:
        runner.verify_frozen_schema_at_head(c, revision="0031")


@pytest.mark.parametrize("revision", ["0030", "0033", "unknown", "multiple", "empty"])
def test_other_revision_refused_without_upgrade(database, monkeypatch, revision):
    engine, _ = database
    with engine.begin() as c:
        if revision == "empty":
            c.exec_driver_sql("DELETE FROM public.alembic_version")
        elif revision == "multiple":
            c.exec_driver_sql("INSERT INTO public.alembic_version VALUES ('0033')")
        else:
            c.execute(sa.text("UPDATE public.alembic_version SET version_num=:r"), {"r": revision})
    before = _revision(engine)
    def forbidden(*args, **kwargs):
        pytest.fail("upgrade reached for refused revision")
    monkeypatch.setattr(runner.command, "upgrade", forbidden)
    with pytest.raises(runner.MigrationRefused):
        run(engine)
    assert _revision(engine) == before


@pytest.mark.parametrize("state", ["drift", "evidence", "active_control", "disabled_trigger"])
def test_certified_noop_refuses_bad_state(database, state):
    engine, _ = database
    assert run(engine).status == "migrated"
    with engine.begin() as c:
        if state == "drift":
            c.exec_driver_sql("ALTER TABLE public.management_readiness_admissions ADD COLUMN extra integer")
        elif state == "evidence":
            c.execute(sa.text(
                "INSERT INTO public.management_readiness_admissions "
                "(operation,release_commit_sha,release_tree_sha,descriptor_sha256,grant_id,actor_hash) "
                "VALUES ('object_storage_readiness_v1',:commit,:tree,:hash,'fixture',:hash)"
            ), {"commit": "a" * 40, "tree": "b" * 40, "hash": "c" * 64})
        elif state == "active_control":
            c.exec_driver_sql("UPDATE public.routine_publishing_control SET state='LIVE'")
        else:
            c.exec_driver_sql("ALTER TABLE public.management_readiness_admissions DISABLE TRIGGER management_readiness_admissions_immutable")
    with pytest.raises(runner.MigrationRefused):
        run(engine)
    assert _revision(engine) == ["0032"]


@pytest.mark.parametrize("bad", ["signature", "expired", "scope", "operation", "job", "artifact", "development", "enabled"])
def test_authorization_and_target_refusal(database, bad):
    engine, _ = database
    args = authorization(engine)
    if bad == "signature":
        args["signature"] = b"x" * 64
    elif bad == "expired":
        args = authorization(engine, issued_at=time.time()-120, expires_at=time.time()-1)
    elif bad in {"scope", "operation"}:
        args = authorization(engine, **{bad: "development" if bad == "scope" else "alembic_head"})
    elif bad == "job":
        args = authorization(engine, job_id="wrong")
    elif bad == "artifact":
        args = authorization(engine, artifact_sha256="d" * 64)
    elif bad == "development":
        args["policy"] = replace(args["policy"], development_identity=args["policy"].production_identity)
    else:
        args["closed_state"]["PUBLISHING_ENABLED"] = "true"
    with pytest.raises(runner.MigrationRefused):
        run(engine, **args)
    assert_0031_untouched(engine)


def test_wrong_connected_target_and_unpaused_control(database):
    engine, _ = database
    args = authorization(engine)
    policy = replace(args["policy"], production_identity="c" * 64)
    claims = json.loads(args["payload"])
    claims["database_identity"] = policy.production_identity
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    args.update(policy=policy, payload=payload, signature=KEY.sign(payload))
    with pytest.raises(runner.MigrationRefused, match="target identity"):
        run(engine, **args)
    with engine.begin() as c:
        c.exec_driver_sql("UPDATE public.routine_publishing_control SET state='LIVE'")
    with pytest.raises(runner.MigrationRefused, match="PAUSED"):
        run(engine)
    assert _revision(engine) == ["0031"]


def test_development_backend_cannot_hide_behind_production_url_metadata(database):
    engine, _ = database
    args = authorization(engine)
    policy = replace(args["policy"], production_server_identity="e" * 64,
                     development_server_identity=args["policy"].production_server_identity)
    claims = json.loads(args["payload"])
    claims["server_identity"] = policy.production_server_identity
    payload = json.dumps(claims, sort_keys=True, separators=(",", ":")).encode()
    args.update(policy=policy, payload=payload, signature=KEY.sign(payload))
    with pytest.raises(runner.MigrationRefused, match="connected server"):
        run(engine, **args)
    assert_0031_untouched(engine)


def test_canonical_hash_refuses_changed_migration(database, monkeypatch):
    engine, _ = database
    monkeypatch.setattr(runner, "MIGRATION_HASH", "0" * 64)
    with pytest.raises(runner.MigrationRefused, match="canonical"):
        run(engine)
    assert_0031_untouched(engine)


def test_0031_frozen_drift_refused_before_upgrade(database, monkeypatch):
    engine, _ = database
    with engine.begin() as c:
        c.exec_driver_sql("ALTER TABLE public.routine_publishing_control ADD COLUMN drift integer")
    def forbidden(*args, **kwargs):
        pytest.fail("upgrade reached after frozen preflight drift")
    monkeypatch.setattr(runner.command, "upgrade", forbidden)
    with pytest.raises(runner.MigrationRefused):
        run(engine)
    assert _revision(engine) == ["0031"]


def test_later_migration_in_graph_is_never_executed(database, tmp_path, monkeypatch):
    import shutil
    engine, _ = database
    location = tmp_path / "alembic"
    shutil.copytree(BACKEND / "alembic", location)
    (location / "versions" / "0034_fixture_never_run.py").write_text(
        "revision='0034'\ndown_revision='0033'\n"
        "def upgrade(): raise AssertionError('later migration executed')\n"
        "def downgrade(): raise AssertionError('downgrade executed')\n"
    )
    original = runner._alembic_config
    def fixture_config(c):
        cfg = original(c)
        cfg.set_main_option("script_location", str(location))
        return cfg
    monkeypatch.setattr(runner, "_alembic_config", fixture_config)
    assert run(engine).status == "migrated"
    assert _revision(engine) == ["0032"]


def test_evidence_or_paused_change_cannot_race_noop_certification(database, monkeypatch):
    engine, _ = database
    assert run(engine).status == "migrated"
    original = runner._certify
    def certify(c):
        original(c)
        # A second session cannot insert evidence or change PAUSED while the
        # first holds certification-to-commit relation locks.
        for sql in (
            "LOCK TABLE public.management_readiness_admissions IN ROW EXCLUSIVE MODE NOWAIT",
            "LOCK TABLE public.routine_publishing_control IN ROW EXCLUSIVE MODE NOWAIT",
        ):
            with engine.connect() as other:
                with pytest.raises(sa.exc.OperationalError):
                    other.exec_driver_sql(sql)
                other.rollback()
    monkeypatch.setattr(runner, "_certify", certify)
    assert run(engine).status == "verified_noop"


def test_one_connection_transaction_and_lock_through_certification(database, monkeypatch):
    engine, _ = database
    original = runner.verify_frozen_schema_at_head
    seen = []
    txs = []
    pids = []
    def verify(c, revision):
        seen.append(c)
        txs.append(c.get_transaction())
        pids.append(c.scalar(sa.text("SELECT pg_backend_pid()")))
        return original(c, revision=revision)
    monkeypatch.setattr(runner, "verify_frozen_schema_at_head", verify)
    assert run(engine).status == "migrated"
    assert len(seen) == 2 and seen[0] is seen[1] and txs[0] is txs[1]
    assert pids[0] == pids[1] and not txs[0].is_active


def test_commit_acknowledgement_failure_stops_without_retry_or_reversal(database, monkeypatch):
    engine, _ = database
    original = sa.engine.RootTransaction.commit
    calls = []
    def lose_ack(tx):
        calls.append(tx)
        original(tx)
        raise OSError("injected lost commit acknowledgement")
    monkeypatch.setattr(sa.engine.RootTransaction, "commit", lose_ack)
    with pytest.raises(runner.CommitOutcomeUnknown):
        run(engine)
    monkeypatch.setattr(sa.engine.RootTransaction, "commit", original)
    assert len(calls) == 1
    assert _revision(engine) == ["0032"]
    with engine.connect() as c:
        runner.verify_frozen_schema_at_head(c, revision="0032")


def test_supplied_env_refuses_stamp_and_no_lock(database):
    engine, _ = database
    with engine.connect() as c:
        tx = c.begin()
        cfg = runner._alembic_config(c)
        with pytest.raises(RuntimeError, match="lock"):
            command.upgrade(cfg, "0032")
        tx.rollback()
    with engine.connect() as c:
        tx = c.begin()
        acquire_transaction_lock(c, 1)
        try:
            with pytest.raises(RuntimeError, match="authorized exact"):
                command.stamp(runner._alembic_config(c), "0032")
        finally:
            tx.rollback()
            clear_lock_proof(c)
    assert_0031_untouched(engine)


@pytest.mark.parametrize("operation", ["stamp", "downgrade"])
def test_authorized_supplied_env_still_refuses_other_operations(database, monkeypatch, operation):
    engine, _ = database
    forbidden_operation = getattr(command, operation)
    monkeypatch.setattr(runner.command, "upgrade", forbidden_operation)
    with pytest.raises(runner.MigrationRefused):
        run(engine)
    assert_0031_untouched(engine)


def test_import_does_not_load_app_engine_or_provider():
    code = (
        "import sys,socket\n"
        "def forbidden(*a,**k): raise AssertionError('network forbidden')\n"
        "socket.getaddrinfo=forbidden; socket.socket.connect=forbidden\n"
        "import app.db.exact_revision_runner\n"
        "assert 'app.db.session' not in sys.modules\n"
        "assert 'app.main' not in sys.modules\n"
        "assert not any(k.startswith(('app.providers','app.services','app.api')) for k in sys.modules)\n"
    )
    r = subprocess.run([sys.executable, "-B", "-c", code], env=_safe_env("sqlite:///:memory:"),
                       cwd=BACKEND, capture_output=True, text=True)
    assert r.returncode == 0, "runner import isolation failed"


def test_full_run_never_imports_or_constructs_runtime_or_provider(database):
    engine, _ = database
    code = '''
import sys, socket, importlib.abc
def forbidden(*a, **k): raise AssertionError("network/provider activation forbidden")
socket.getaddrinfo = forbidden
socket.socket.connect = forbidden
socket.socket.connect_ex = forbidden
class Fence(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        if fullname == "app.db.session" or fullname.startswith((
            "app.main", "app.api", "app.providers",
            "app.services.routine_", "app.services.pinterest_",
            "app.services.object_storage_readiness", "app.integrations",
        )):
            raise AssertionError("runtime/provider import forbidden")
        return None
sys.meta_path.insert(0, Fence())
import replit.object_storage as storage
storage.Client = forbidden
import httpx
httpx.Client = forbidden
httpx.AsyncClient = forbidden
import sqlalchemy as sa, json, time
from cryptography.hazmat.primitives.asymmetric.ed25519 import Ed25519PrivateKey
from cryptography.hazmat.primitives.serialization import Encoding, PublicFormat
from app.db import exact_revision_runner as r
from app.db.database_identity import database_identity_sha256, connected_database_identity_sha256
engine = sa.create_engine(TARGET_URL)
key = Ed25519PrivateKey.generate()
identity = database_identity_sha256(TARGET_URL)
with engine.connect() as c:
    server_identity = connected_database_identity_sha256(c)
policy = r.InvocationPolicy(
    key.public_key().public_bytes(Encoding.Raw, PublicFormat.Raw),
    "fixture-app", "a"*64, identity, "b"*64, "fixture-job", server_identity, "d"*64,
)
claims = {
    "operation":"alembic_0031_to_0032", "scope":"production",
    "invocation":"platform_migration_job", "app_id":policy.app_id,
    "artifact_sha256":policy.artifact_sha256, "database_identity":identity,
    "server_identity":policy.production_server_identity,
    "job_id":policy.job_id, "issued_at":time.time()-1, "expires_at":time.time()+240,
}
payload=json.dumps(claims,sort_keys=True,separators=(",",":")).encode()
closed={k:"false" for k in r.DISABLED_GATES}
closed.update(APP_ENV="production",AI_PROVIDER="none",ROUTINE_PINTEREST_DRY_RUN="true")
with engine.connect() as c:
    assert r.run_exact_0032(
        c,c.begin(),payload=payload,signature=key.sign(payload),policy=policy,
        closed_state=closed,
    ).status=="migrated"
engine.dispose()
assert "app.main" not in sys.modules and "app.db.session" not in sys.modules
'''
    code = "TARGET_URL=" + repr(engine.url.render_as_string(hide_password=False)) + "\n" + code
    result = subprocess.run([sys.executable, "-B", "-c", code],
                            env=_safe_env("sqlite:///:memory:"), cwd=BACKEND,
                            capture_output=True, text=True)
    assert result.returncode == 0, "full runner isolation fence failed"
    assert _revision(engine) == ["0032"]


def test_fixed_transition_has_no_arbitrary_revision_argument():
    import inspect
    assert "revision" not in inspect.signature(runner.run_exact_0032).parameters
    assert "target" not in inspect.signature(runner.run_exact_0032).parameters
    source = inspect.getsource(runner)
    assert 'command.upgrade(cfg, "0032")' in source
    assert "command.downgrade(" not in source and "command.stamp(" not in source