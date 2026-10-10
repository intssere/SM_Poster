"""Real Unix-socket disposable PostgreSQL; no attached DB or provider access."""
from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
import hashlib
import json
from pathlib import Path

import pytest
import sqlalchemy as sa
from sqlalchemy.schema import CreateTable
from sqlalchemy.dialects import postgresql

from app.db import readiness_schema_0033 as frozen
from app.db.migration_adoption import (
    SchemaAdoptionRefused, _catalog_contract, _fingerprint,
    adopt_managed_preapplied_0033, verify_frozen_schema_at_head,
)
from app.services.readiness_execution_admission import (
    PostgresReadinessAdmission, management_readiness_admissions as admissions,
    management_readiness_outcomes as outcomes, metadata,
)
from app.services.readiness_execution_contract import ReadinessError
from test_readiness_execution_admission_0032 import (
    _isolated_database, _binding, _revision, _run_alembic, _runner_receipt,
    pytestmark,
)


def legacy_insert(engine, *, release="d", outcome="ADMITTED", receipt=None, exit_code=None):
    """Insert through the historical guards, not the new coordinator."""
    binding = _binding(release)
    with engine.begin() as c:
        table = sa.Table(frozen.TABLES[0], sa.MetaData(), schema="public", autoload_with=c)
        c.execute(table.insert().values(
            operation=binding.scope[0], release_commit_sha=binding.scope[1],
            release_tree_sha=binding.scope[2], descriptor_sha256=binding.digest,
            grant_id=f"grant-{release}", actor_hash="a" * 64,
        ))
        if outcome != "ADMITTED":
            c.execute(table.update().values(
                outcome=outcome, receipt=sa.null() if receipt is None else receipt, exit_code=exit_code,
                finished_at=sa.func.now(),
            ).where(table.c.release_commit_sha == binding.release_commit_sha))
    return binding


def preapply(engine):
    # Managed-preview stand-in: DDL only, no data or version synchronization.
    with engine.begin() as c:
        for table in metadata.sorted_tables:
            c.execute(CreateTable(table))


def adopt(engine):
    with engine.begin() as c:
        return adopt_managed_preapplied_0033(c)


def business_evidence(engine):
    with engine.connect() as c:
        result = {}
        for name in sa.inspect(c).get_table_names(schema="public"):
            if name in (*frozen.TABLES, "alembic_version"):
                continue
            rows = c.execute(sa.text(
                f'SELECT to_jsonb(t)::text FROM public."{name}" t ORDER BY to_jsonb(t)::text'
            )).scalars().all()
            result[name] = (_fingerprint(_catalog_contract(c, name)), rows)
        return result


def test_metadata_matches_reviewed_frozen_catalog():
    with _isolated_database("0031") as (engine, _):
        preapply(engine)
        with engine.connect() as c:
            actual = {t: _fingerprint(_catalog_contract(c, t)) for t in frozen.TABLES}
            assert actual == frozen.FINGERPRINTS, json.dumps({
                "hashes": actual,
                "catalogs": {t: _catalog_contract(c, t) for t in frozen.TABLES},
            }, indent=2)
            frozen.verify(c)


@pytest.mark.parametrize("states", [
    (), ("ADMITTED",), ("PASS",), ("FAILED",), ("UNKNOWN",),
    ("ADMITTED", "PASS", "FAILED", "UNKNOWN", "BLOCKED"),
])
def test_lossless_migration_and_business_preservation(states):
    with _isolated_database("0032") as (engine, url):
        with engine.begin() as c:
            c.execute(sa.text(
                "INSERT INTO stores (id,name,shop_domain,market) "
                "VALUES ('business-fixture','Preserved','fixture.invalid','US')"
            ))
        before = business_evidence(engine)
        for i, state in enumerate(states):
            status = "FAILED" if state == "BLOCKED" else state
            receipt = None if state in {"ADMITTED", "UNKNOWN"} else _runner_receipt(
                state, None if state == "PASS" else (
                    "GATE_DISABLED" if state == "BLOCKED" else "SDK_OPERATION_FAILED"
                ),
            )
            legacy_insert(engine, release=str(i), outcome=status, receipt=receipt,
                          exit_code={"PASS": 0, "FAILED": 1, "BLOCKED": 2}.get(state))
        with engine.connect() as c:
            original = c.execute(sa.text(
                "SELECT * FROM public.management_readiness_admissions ORDER BY release_commit_sha"
            )).mappings().all()
        result = _run_alembic(url, "0033")
        assert result.returncode == 0, result.stderr
        assert _revision(engine) == ["0033"]
        assert business_evidence(engine) == before
        with engine.connect() as c:
            verify_frozen_schema_at_head(c, "0033")
            current = c.execute(sa.select(admissions).order_by(
                admissions.c.release_commit_sha
            )).mappings().all()
            assert [dict(r) for r in current] == [
                {k: r[k] for k in admissions.c.keys()} for r in original
            ]
            terminal = c.execute(sa.select(outcomes).order_by(
                outcomes.c.release_commit_sha
            )).mappings().all()
            assert [dict(r) for r in terminal] == [
                {k: r[k] for k in outcomes.c.keys()}
                for r in original if r["outcome"] != "ADMITTED"
            ]


def test_invalid_receipt_refuses_without_removing_guard_or_evidence():
    with _isolated_database("0032") as (engine, url):
        # Valid shallow old DB check, invalid full application receipt.
        legacy_insert(engine, outcome="PASS", receipt={"final_status": "PASS"}, exit_code=0)
        result = _run_alembic(url, "0033")
        assert result.returncode != 0
        assert _revision(engine) == ["0032"]
        with engine.connect() as c:
            verify_frozen_schema_at_head(c, "0032")
            assert c.scalar(sa.text("SELECT count(*) FROM public.management_readiness_admissions")) == 1
            assert c.scalar(sa.text("SELECT to_regclass('public.management_readiness_outcomes')")) is None


def test_invalid_consumption_refuses_without_changing_historical_state():
    with _isolated_database("0032") as (engine, url):
        with engine.begin() as c:
            c.execute(sa.text(
                "INSERT INTO public.management_readiness_admissions "
                "(operation,release_commit_sha,release_tree_sha,descriptor_sha256,grant_id,actor_hash) "
                "VALUES ('object_storage_readiness_v1','invalid',:tree,:digest,'',:actor)"
            ), {"tree": "e"*40, "digest": "a"*64, "actor": "b"*64})
        result = _run_alembic(url, "0033")
        assert result.returncode != 0
        assert _revision(engine) == ["0032"]
        with engine.connect() as c:
            verify_frozen_schema_at_head(c, "0032")
            assert c.scalar(sa.text("SELECT count(*) FROM public.management_readiness_admissions")) == 1


def test_development_preexisting_outcomes_refused():
    with _isolated_database("0032") as (engine, url):
        with engine.begin() as c:
            c.execute(sa.text("CREATE TABLE public.management_readiness_outcomes (unexpected int)"))
        result = _run_alembic(url, "0033")
        assert result.returncode != 0
        assert _revision(engine) == ["0032"]


def test_empty_managed_adoption_only_changes_bookkeeping_and_is_idempotent():
    with _isolated_database("0031") as (engine, _):
        before = business_evidence(engine)
        preapply(engine)
        statements = []
        def record(_c, _cur, sql, _p, _ctx, _many):
            statements.append(sql.strip().upper())
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            assert adopt(engine) is True
            assert adopt(engine) is False
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        writes = [s for s in statements if s.startswith(
            ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ", "TRUNCATE ")
        )]
        assert len(writes) == 1 and writes[0].startswith("UPDATE PUBLIC.ALEMBIC_VERSION ")
        assert _revision(engine) == ["0033"]
        assert business_evidence(engine) == before


@pytest.mark.parametrize("ddl", [
    "DROP TABLE public.management_readiness_outcomes",
    "ALTER TABLE public.management_readiness_admissions ADD COLUMN extra int",
    "ALTER TABLE public.management_readiness_outcomes ALTER COLUMN finished_at DROP DEFAULT",
    "ALTER TABLE public.management_readiness_admissions ALTER COLUMN actor_hash TYPE varchar(63)",
    "ALTER TABLE public.management_readiness_outcomes DROP CONSTRAINT ck_management_readiness_outcomes_evidence",
    "ALTER TABLE public.management_readiness_outcomes DROP CONSTRAINT fk_management_readiness_outcomes_admission",
    "ALTER TABLE public.management_readiness_outcomes DROP CONSTRAINT fk_management_readiness_outcomes_admission; "
    "ALTER TABLE public.management_readiness_outcomes ADD CONSTRAINT fk_management_readiness_outcomes_admission "
    "FOREIGN KEY (operation,release_commit_sha,release_tree_sha) REFERENCES public.management_readiness_admissions "
    "ON DELETE CASCADE",
    "CREATE INDEX unexpected_readiness_index ON public.management_readiness_admissions(actor_hash)",
    "ALTER TABLE public.management_readiness_outcomes DROP CONSTRAINT ck_management_readiness_outcomes_outcome; "
    "ALTER TABLE public.management_readiness_outcomes ADD CONSTRAINT ck_management_readiness_outcomes_outcome "
    "CHECK (outcome IN ('ADMITTED','PASS','FAILED','UNKNOWN'))",
    "ALTER TABLE public.management_readiness_admissions DROP CONSTRAINT uq_management_readiness_admissions_grant; "
    "ALTER TABLE public.management_readiness_admissions ADD CONSTRAINT uq_management_readiness_admissions_grant "
    "UNIQUE(grant_id, actor_hash)",
    "ALTER TABLE public.management_readiness_outcomes ENABLE ROW LEVEL SECURITY",
    "CREATE FUNCTION public.management_readiness_unexpected() RETURNS int LANGUAGE sql AS $$ SELECT 1 $$",
    "CREATE FUNCTION public.management_readiness_admission_guard() RETURNS trigger "
    "LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$",
    "CREATE FUNCTION public.unexpected_readiness_guard() RETURNS trigger LANGUAGE plpgsql AS $$ BEGIN RETURN NEW; END $$; "
    "CREATE TRIGGER unexpected BEFORE INSERT ON public.management_readiness_outcomes "
    "FOR EACH ROW EXECUTE FUNCTION public.unexpected_readiness_guard()",
])
def test_exact_verifier_and_adopter_refuse_drift(ddl):
    with _isolated_database("0031") as (engine, _):
        preapply(engine)
        with engine.begin() as c:
            c.exec_driver_sql(ddl)
        with pytest.raises(SchemaAdoptionRefused):
            adopt(engine)
        assert _revision(engine) == ["0031"]


@pytest.mark.parametrize("versions", [[], ["0032"], ["0034"], ["0031", "0032"]])
def test_adoption_revision_refusal(versions):
    with _isolated_database("0031") as (engine, _):
        preapply(engine)
        with engine.begin() as c:
            c.execute(sa.text("DELETE FROM public.alembic_version"))
            for version in versions:
                c.execute(sa.text("INSERT INTO public.alembic_version VALUES (:v)"), {"v": version})
        with pytest.raises(SchemaAdoptionRefused):
            adopt(engine)
        assert _revision(engine) == sorted(versions)


@pytest.mark.parametrize("terminal", [False, True])
def test_nonempty_preapply_refused(terminal):
    with _isolated_database("0031") as (engine, _):
        preapply(engine)
        with engine.begin() as c:
            b = _binding()
            c.execute(admissions.insert().values(
                operation=b.scope[0], release_commit_sha=b.scope[1], release_tree_sha=b.scope[2],
                descriptor_sha256=b.digest, grant_id="grant", actor_hash="a"*64,
            ))
            if terminal:
                c.execute(outcomes.insert().values(
                    operation=b.scope[0], release_commit_sha=b.scope[1], release_tree_sha=b.scope[2],
                    outcome="UNKNOWN",
                ))
        with pytest.raises(SchemaAdoptionRefused):
            adopt(engine)
        assert _revision(engine) == ["0031"]


def test_paused_and_missing_schema_refusal():
    with _isolated_database("0031") as (engine, _):
        with pytest.raises(SchemaAdoptionRefused):
            adopt(engine)
        preapply(engine)
        with engine.begin() as c:
            c.execute(sa.text("DELETE FROM public.routine_publishing_control"))
        with pytest.raises(SchemaAdoptionRefused, match="PAUSED"):
            adopt(engine)
        assert _revision(engine) == ["0031"]


@pytest.mark.parametrize("state", ["DRY_RUN", None])
def test_nonpaused_or_missing_control_refuses_without_business_writes(state):
    with _isolated_database("0031") as (engine, _):
        preapply(engine)
        with engine.begin() as c:
            if state is None:
                c.execute(sa.text("DELETE FROM public.routine_publishing_control"))
            else:
                c.execute(sa.text(
                    "UPDATE public.routine_publishing_control SET state=:state"
                ), {"state": state})
        before = business_evidence(engine)
        with pytest.raises(SchemaAdoptionRefused, match="PAUSED"):
            adopt(engine)
        assert business_evidence(engine) == before
        assert _revision(engine) == ["0031"]


@pytest.mark.parametrize("activity", ["PUBLISHING", "PUBLISH_UNKNOWN", "RUNNING", "ACTIVE"])
def test_live_state_refusal(activity):
    with _isolated_database("0031") as (engine, _):
        preapply(engine)
        with engine.begin() as c:
            # Disposable cluster only, modeling activity without providers.
            c.execute(sa.text("SET LOCAL session_replication_role = replica"))
            if activity in {"PUBLISHING", "PUBLISH_UNKNOWN"}:
                c.execute(sa.text(
                    "INSERT INTO pin_publications (id,draft_id,creative_id,"
                    "publication_fingerprint,status,provider_response) "
                    "VALUES ('pub','draft','creative','fingerprint',:status,'{}')"
                ), {"status": activity})
            elif activity == "RUNNING":
                c.execute(sa.text(
                    "INSERT INTO routine_publishing_runs (id,mode,started_at,status,"
                    "scanned,eligible,skipped,claimed,dispatched,published,failed,unknown,metadata_json) VALUES "
                    "('run','DRY_RUN',now(),'RUNNING',0,0,0,0,0,0,0,0,'{}')"
                ))
            else:
                c.execute(sa.text(
                    "INSERT INTO routine_dispatch_permits "
                    "(id,publication_id,dispatch_provider,approval_id,"
                    "pinterest_board_record_id,publication_fingerprint,request_fingerprint,"
                    "scheduled_for_snapshot,quality_policy_version,quality_snapshot,"
                    "duplicate_snapshot,readiness_snapshot,authorized_by,authorized_at,"
                    "expires_at,status) VALUES "
                    "('permit','pub','buffer','approval','board','fingerprint','request',"
                    "now(),'v1','{}','{}','{}','test',now(),now()+interval '1 hour','ACTIVE')"
                ))
        before = business_evidence(engine)
        with pytest.raises(SchemaAdoptionRefused, match="live state"):
            adopt(engine)
        assert business_evidence(engine) == before
        assert _revision(engine) == ["0031"]


def test_adoption_rollback_and_lock_contention_leave_bookkeeping_untouched():
    with _isolated_database("0031") as (engine, _):
        preapply(engine)
        with pytest.raises(RuntimeError, match="rollback"):
            with engine.begin() as c:
                assert adopt_managed_preapplied_0033(c)
                raise RuntimeError("rollback")
        assert _revision(engine) == ["0031"]
        with engine.begin() as c:
            c.execute(sa.text("LOCK TABLE public.management_readiness_outcomes IN ACCESS EXCLUSIVE MODE"))
            with pytest.raises(SchemaAdoptionRefused):
                adopt(engine)
        assert _revision(engine) == ["0031"]


def test_fresh_guard_and_startup_adoption_are_schema_only(monkeypatch):
    from app.db import managed_schema_adoption, schema_canonicality_guard
    with _isolated_database("0031") as (engine, _):
        preapply(engine)
        monkeypatch.setattr(managed_schema_adoption, "engine", engine)
        monkeypatch.setattr(schema_canonicality_guard, "engine", engine)
        before = business_evidence(engine)
        assert managed_schema_adoption.main() == 0
        # Historical adoption stops at 0033 and cannot bypass the new head.
        with pytest.raises(SchemaAdoptionRefused, match="Alembic revision 0035"):
            schema_canonicality_guard.main()
        assert business_evidence(engine) == before
        assert PostgresReadinessAdmission(engine).lookup(_binding()) is None


def test_wrong_schema_alias_and_incoming_business_fk_refuse():
    with _isolated_database("0031") as (engine, _):
        preapply(engine)
        with engine.begin() as c:
            c.execute(sa.text(
                "CREATE SCHEMA shadow; CREATE TABLE shadow.management_readiness_outcomes(id int)"
            ))
        with pytest.raises(SchemaAdoptionRefused):
            adopt(engine)
        with engine.begin() as c:
            c.execute(sa.text("DROP SCHEMA shadow CASCADE"))
            c.execute(sa.text(
                "ALTER TABLE stores ADD COLUMN readiness_operation varchar(64),"
                "ADD COLUMN readiness_commit varchar(64),ADD COLUMN readiness_tree varchar(64),"
                "ADD CONSTRAINT unexpected_business_readiness_fk FOREIGN KEY "
                "(readiness_operation,readiness_commit,readiness_tree) "
                "REFERENCES public.management_readiness_admissions"
            ))
        with pytest.raises(SchemaAdoptionRefused):
            adopt(engine)
        assert _revision(engine) == ["0031"]


def test_coordinator_only_inserts_one_consumption_and_one_terminal():
    with _isolated_database("0033") as (engine, _):
        store = PostgresReadinessAdmission(engine)
        b = _binding()
        statements = []
        def record(_c, _cur, sql, _p, _ctx, _many):
            statements.append(sql.strip().upper())
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            with ThreadPoolExecutor(max_workers=4) as pool:
                assert sum(pool.map(lambda _: store.consume(b, "race", "a"*64), range(4))) == 1
            assert not store.consume(_binding("9"), "race", "a"*64)
            assert store.lookup(b)["outcome"] == "UNKNOWN"
            receipt = _runner_receipt()
            with ThreadPoolExecutor(max_workers=3) as pool:
                list(pool.map(lambda _: store.finish(b, "PASS", 0, receipt), range(3)))
            with engine.connect() as c:
                timestamp = c.scalar(sa.select(outcomes.c.finished_at))
            store.finish(b, "PASS", 0, receipt)
            with pytest.raises(ReadinessError, match="OUTCOME_PERSISTENCE_FAILED"):
                store.finish(b, "UNKNOWN", None, None)
            assert store.lookup(b)["receipt"] == receipt
            with engine.connect() as c:
                assert c.scalar(sa.select(outcomes.c.finished_at)) == timestamp
                assert c.scalar(sa.select(sa.func.count()).select_from(outcomes)) == 1
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        assert not any(s.startswith(("UPDATE ", "DELETE ", "TRUNCATE ")) for s in statements)


def test_racing_different_completions_and_mismatched_binding_fail_closed():
    from dataclasses import replace
    with _isolated_database("0033") as (engine, _):
        store, b = PostgresReadinessAdmission(engine), _binding()
        with pytest.raises(ReadinessError, match="OUTCOME_PERSISTENCE_FAILED"):
            store.finish(b, "UNKNOWN", None, None)
        assert store.consume(b, "different-finishes", "a"*64)
        with pytest.raises(ReadinessError, match="OUTCOME_PERSISTENCE_FAILED"):
            store.finish(replace(b, overlay_sha256="9"*64), "UNKNOWN", None, None)
        def finish(value):
            try:
                store.finish(b, *value)
                return True
            except ReadinessError as exc:
                assert exc.code == "OUTCOME_PERSISTENCE_FAILED"
                return False
        with ThreadPoolExecutor(max_workers=2) as pool:
            assert sum(pool.map(finish, [
                ("PASS", 0, _runner_receipt()), ("UNKNOWN", None, None),
            ])) == 1
        with engine.connect() as c:
            assert c.scalar(sa.select(sa.func.count()).select_from(outcomes)) == 1


@pytest.mark.parametrize("outcome,exit_code,receipt", [
    ("PASS", None, {"final_status": "PASS"}),
    ("PASS", 0, None), ("PASS", 0, {"final_status": "FAILED"}),
    ("FAILED", 1, {"final_status": "BLOCKED"}),
    ("FAILED", 2, {"final_status": "FAILED"}),
    ("UNKNOWN", None, {"final_status": "PASS"}),
    ("PASS", 0, []), ("PASS", 0, "null"),
])
def test_evidence_check_fails_closed_on_null_and_mismatch(outcome, exit_code, receipt):
    with _isolated_database("0033") as (engine, _):
        b = _binding()
        store = PostgresReadinessAdmission(engine)
        assert store.consume(b, "check", "a"*64)
        with pytest.raises(sa.exc.DBAPIError):
            with engine.begin() as c:
                c.execute(outcomes.insert().values(
                    operation=b.scope[0], release_commit_sha=b.scope[1], release_tree_sha=b.scope[2],
                    outcome=outcome, exit_code=exit_code, receipt=receipt,
                ))
        assert store.lookup(b)["outcome"] == "UNKNOWN"


@pytest.mark.parametrize("terminal", [False, True])
def test_downgrade_refuses_any_evidence(terminal):
    with _isolated_database("0033") as (engine, url):
        store = PostgresReadinessAdmission(engine)
        b = _binding()
        assert store.consume(b, "downgrade", "a"*64)
        if terminal:
            store.finish(b, "UNKNOWN", None, None)
        result = _run_alembic(url, "0032", direction="downgrade")
        assert result.returncode != 0
        assert _revision(engine) == ["0033"]
        assert store.lookup(b)["outcome"] == "UNKNOWN"


def test_empty_downgrade_restores_exact_historical_schema_and_hash():
    with _isolated_database("0033") as (engine, url):
        result = _run_alembic(url, "0032", direction="downgrade")
        assert result.returncode == 0, result.stderr
        with engine.connect() as c:
            verify_frozen_schema_at_head(c, "0032")
        path = Path(__file__).parents[1] / "alembic/versions/0032_object_storage_readiness_management.py"
        assert hashlib.sha256(path.read_bytes()).hexdigest() == frozen.HISTORICAL_0032_SHA256


def test_managed_fixture_has_only_structural_ddl_and_no_business_foreign_keys():
    ddl = [str(CreateTable(t).compile(dialect=postgresql.dialect())) for t in metadata.sorted_tables]
    assert len(ddl) == 2
    assert all("public.management_readiness_" in sql for sql in ddl)
    assert not any(token in " ".join(ddl).upper() for token in ("CREATE FUNCTION", "CREATE TRIGGER"))
    assert len(outcomes.foreign_key_constraints) == 1
    assert all(fk.column.table is admissions for fk in outcomes.foreign_keys)


def test_constraints_only_are_not_claimed_as_sql_immutability():
    with _isolated_database("0033") as (engine, _):
        b = _binding()
        assert PostgresReadinessAdmission(engine).consume(b, "trust-boundary", "a"*64)
        with engine.begin() as c:
            # Explicit regression evidence of the narrower trust boundary.
            c.execute(admissions.update().values(actor_hash="b"*64))
            c.execute(admissions.delete())
        assert PostgresReadinessAdmission(engine).lookup(b) is None