"""Executable production-mode migrations on isolated disposable PostgreSQL."""
from __future__ import annotations

import subprocess
import sys

import pytest
import sqlalchemy as sa

from app.db.migration_adoption import verify_frozen_schema_at_head
from tests.test_readiness_execution_admission_0033 import (
    adopt, business_evidence, legacy_insert, preapply,
)
from tests.test_readiness_execution_admission_0032 import (
    BACKEND, _isolated_database, _revision, _safe_env, pytestmark,
)


def _production_alembic(url, target="head", direction="upgrade", environment="production"):
    return subprocess.run(
        [sys.executable, "-m", "alembic", direction, target],
        cwd=BACKEND,
        env=_safe_env(url, extra={"APP_ENV": environment}),
        capture_output=True, text=True, check=False,
    )


@pytest.mark.parametrize("environment", ["production", "replit", "staging"])
def test_empty_database_production_cli_reaches_canonical_head(environment):
    with _isolated_database("base") as (engine, url):
        result = _production_alembic(url, environment=environment)
        assert result.returncode == 0, result.stderr
        assert "Running upgrade 0032 -> 0033" in result.stderr
        assert "Running upgrade 0033 -> 0034" in result.stderr
        assert _revision(engine) == ["0034"]
        with engine.connect() as connection:
            verify_frozen_schema_at_head(connection, revision="0034")


def test_existing_canonical_0032_executable_progression_preserves_business():
    with _isolated_database("0032") as (engine, url):
        with engine.begin() as connection:
            connection.execute(sa.text(
                "INSERT INTO stores (id,name,shop_domain,market) "
                "VALUES ('canonical-business','Preserved','fixture.invalid','US')"
            ))
        before = business_evidence(engine)
        for revision in ("0033", "0034"):
            result = _production_alembic(url, revision)
            assert result.returncode == 0, result.stderr
            assert _revision(engine) == [revision]
            with engine.connect() as connection:
                verify_frozen_schema_at_head(connection, revision=revision)
        # 0034 adds controller tables; all pre-existing business evidence stays exact.
        after = business_evidence(engine)
        assert {name: after[name] for name in before} == before


@pytest.mark.parametrize("ddl", [
    "DROP TABLE public.management_readiness_admissions",
    "ALTER TABLE public.management_readiness_admissions ADD COLUMN tampered int",
    "ALTER TABLE public.management_readiness_admissions ALTER COLUMN actor_hash DROP NOT NULL",
    "ALTER TABLE public.management_readiness_admissions DISABLE TRIGGER management_readiness_admissions_immutable",
    "ALTER TABLE public.management_readiness_admissions ENABLE ROW LEVEL SECURITY",
    "ALTER TABLE public.pinterest_seo_briefs ADD COLUMN tampered int",
    "CREATE TABLE public.management_readiness_outcomes (unexpected int)",
])
def test_production_noncanonical_or_preapplied_readiness_refuses_without_changes(ddl):
    with _isolated_database("0032") as (engine, url):
        with engine.begin() as connection:
            connection.execute(sa.text(ddl))
        before = business_evidence(engine)
        result = _production_alembic(url, "0033")
        assert result.returncode != 0
        assert "development/test only" not in result.stderr
        assert _revision(engine) == ["0032"]
        assert business_evidence(engine) == before
        with engine.connect() as connection:
            present = connection.scalar(sa.text(
                "SELECT to_regclass('public.management_readiness_outcomes')"
            ))
            assert bool(present) == ("CREATE TABLE" in ddl)


@pytest.mark.parametrize("outcome", ["ADMITTED", "UNKNOWN"])
def test_production_nonempty_readiness_refuses_and_preserves_evidence(outcome):
    with _isolated_database("0032") as (engine, url):
        legacy_insert(engine, outcome=outcome)
        with engine.connect() as connection:
            before = connection.execute(sa.text(
                "SELECT to_jsonb(a)::text FROM public.management_readiness_admissions a"
            )).scalars().all()
        result = _production_alembic(url, "0033")
        assert result.returncode != 0
        assert "requires empty canonical readiness admissions" in result.stderr
        assert _revision(engine) == ["0032"]
        with engine.connect() as connection:
            verify_frozen_schema_at_head(connection, revision="0032")
            assert connection.execute(sa.text(
                "SELECT to_jsonb(a)::text FROM public.management_readiness_admissions a"
            )).scalars().all() == before
            assert connection.scalar(sa.text(
                "SELECT to_regclass('public.management_readiness_outcomes')"
            )) is None


def test_exact_managed_preapply_adopts_then_production_executes_0034():
    with _isolated_database("0031") as (engine, url):
        preapply(engine)
        before = business_evidence(engine)
        statements = []
        def record(_c, _cur, sql, _parameters, _context, _many):
            statements.append(sql.strip().upper())
        sa.event.listen(engine, "before_cursor_execute", record)
        try:
            assert adopt(engine) is True
            assert adopt(engine) is False
        finally:
            sa.event.remove(engine, "before_cursor_execute", record)
        writes = [sql for sql in statements if sql.startswith(
            ("INSERT ", "UPDATE ", "DELETE ", "CREATE ", "ALTER ", "DROP ", "TRUNCATE ")
        )]
        assert len(writes) == 1
        assert writes[0].startswith("UPDATE PUBLIC.ALEMBIC_VERSION ")
        assert _revision(engine) == ["0033"]
        assert business_evidence(engine) == before
        result = _production_alembic(url, "0034")
        assert result.returncode == 0, result.stderr
        with engine.connect() as connection:
            verify_frozen_schema_at_head(connection, revision="0034")


def test_exact_preapplied_0033_schema_is_not_executably_adopted_from_0031():
    with _isolated_database("0031") as (engine, url):
        preapply(engine)
        result = _production_alembic(url, "head")
        assert result.returncode != 0
        assert _revision(engine) == ["0031"]


def test_production_downgrade_remains_restricted():
    with _isolated_database("0033") as (engine, url):
        result = _production_alembic(url, "0032", direction="downgrade")
        assert result.returncode != 0
        assert "0033 executable migration is development/test only" in result.stderr
        assert _revision(engine) == ["0033"]
        with engine.connect() as connection:
            verify_frozen_schema_at_head(connection, revision="0033")


def test_supported_empty_downgrade_and_production_reupgrade():
    with _isolated_database("0034") as (engine, url):
        result = subprocess.run(
            [sys.executable, "-m", "alembic", "downgrade", "0032"],
            cwd=BACKEND, env=_safe_env(url),
            capture_output=True, text=True, check=False,
        )
        assert result.returncode == 0, result.stderr
        with engine.connect() as connection:
            verify_frozen_schema_at_head(connection, revision="0032")
        result = _production_alembic(url)
        assert result.returncode == 0, result.stderr
        with engine.connect() as connection:
            verify_frozen_schema_at_head(connection, revision="0034")