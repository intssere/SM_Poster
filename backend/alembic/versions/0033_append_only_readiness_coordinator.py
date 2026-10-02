"""Split immutable consumption from insert-only terminal evidence.

Revision ID: 0033
Revises: 0032

Development/test migration only. Managed production receives structural
changes through Publish, not this executable data transformation.
"""
from __future__ import annotations

import hashlib
import importlib.util
from pathlib import Path

from alembic import op
import sqlalchemy as sa

from app.db import readiness_schema_0033 as frozen
from app.db.migration_adoption import (
    _table_fingerprints, verify_frozen_schema_at_head,
)

revision = "0033"
down_revision = "0032"
branch_labels = None
depends_on = None


def _bind(expected):
    from app.core.config import get_settings

    connection = op.get_bind()
    if connection.dialect.name != "postgresql":
        raise RuntimeError("0033 requires PostgreSQL")
    if get_settings().app_env not in {"development", "test"}:
        raise RuntimeError("0033 executable migration is development/test only")
    connection.execute(sa.text("SET LOCAL search_path TO public, pg_catalog"))
    connection.execute(sa.text("SET LOCAL lock_timeout = '2s'"))
    connection.execute(sa.text("SET LOCAL statement_timeout = '8s'"))
    connection.execute(sa.text(
        'LOCK TABLE public.alembic_version IN ACCESS EXCLUSIVE MODE'
    ))
    verify_frozen_schema_at_head(connection, revision=expected)
    for table in frozen.TABLES:
        if connection.scalar(sa.text("SELECT to_regclass(:name)"),
                             {"name": f"public.{table}"}) is not None:
            connection.execute(sa.text(
                f'LOCK TABLE "public"."{table}" IN ACCESS EXCLUSIVE MODE'
            ))
    return connection


def _historical():
    path = Path(__file__).with_name("0032_object_storage_readiness_management.py")
    if hashlib.sha256(path.read_bytes()).hexdigest() != frozen.HISTORICAL_0032_SHA256:
        raise RuntimeError("historical 0032 migration changed")
    spec = importlib.util.spec_from_file_location("frozen_readiness_0032", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _business(connection):
    names = tuple(t for t in sa.inspect(connection).get_table_names(schema="public")
                  if t not in (*frozen.TABLES, "alembic_version"))
    return _table_fingerprints(connection, names)


def upgrade():
    _historical()
    connection = _bind("0032")
    if connection.scalar(sa.text(
        "SELECT to_regclass('public.management_readiness_outcomes')"
    )) is not None:
        raise RuntimeError("0033 refuses pre-existing outcomes")
    before = _business(connection)
    original = connection.execute(sa.text(
        "SELECT * FROM public.management_readiness_admissions"
    )).mappings().all()
    for row in original:
        frozen.validate_consumption(row)
        if row["outcome"] != "ADMITTED":
            frozen.validate_terminal(row["outcome"], row["exit_code"], row["receipt"])
    metadata = sa.MetaData()
    _, outcomes = frozen.tables(metadata)
    outcomes.create(connection)
    terminal = [{
        **{column: row[column] for column in frozen.SCOPE},
        **{column: row[column] for column in (
            "outcome", "exit_code", "receipt", "finished_at",
        )},
    } for row in original if row["outcome"] != "ADMITTED"]
    if terminal:
        connection.execute(outcomes.insert(), terminal)
    copied = connection.execute(sa.select(outcomes)).mappings().all()
    if sorted((dict(row) for row in copied), key=lambda r: tuple(r[c] for c in frozen.SCOPE)) != sorted(
        terminal, key=lambda r: tuple(r[c] for c in frozen.SCOPE)
    ):
        raise RuntimeError("0033 evidence copy mismatch")
    # Validation and complete terminal copy precede removal of any guard.
    op.execute("DROP TRIGGER management_readiness_admissions_immutable "
               "ON public.management_readiness_admissions")
    op.execute("DROP TRIGGER management_readiness_admissions_no_truncate "
               "ON public.management_readiness_admissions")
    for name in frozen.LEGACY_FUNCTIONS:
        op.execute(f"DROP FUNCTION public.{name}()")
    for name in ("outcome", "outcome_evidence"):
        op.drop_constraint(f"ck_management_readiness_admissions_{name}",
                           frozen.TABLES[0], schema="public", type_="check")
    for column in ("outcome", "exit_code", "receipt", "finished_at"):
        op.drop_column(frozen.TABLES[0], column, schema="public")
    actual = connection.execute(sa.text(
        "SELECT * FROM public.management_readiness_admissions"
    )).mappings().all()
    expected = [{key: value for key, value in row.items()
                 if key not in {"outcome", "exit_code", "receipt", "finished_at"}}
                for row in original]
    key = lambda r: tuple(r[c] for c in frozen.SCOPE)
    if sorted(map(dict, actual), key=key) != sorted(expected, key=key):
        raise RuntimeError("0033 consumption preservation failed")
    frozen.verify(connection)
    if _business(connection) != before:
        raise RuntimeError("0033 business catalog changed")


def downgrade():
    historical = _historical()
    connection = _bind("0033")
    for table in frozen.TABLES:
        if connection.scalar(sa.text(f'SELECT count(*) FROM public."{table}"')):
            raise RuntimeError("0033 downgrade blocked: readiness evidence exists")
    before = _business(connection)
    op.drop_table(frozen.TABLES[1], schema="public")
    op.drop_table(frozen.TABLES[0], schema="public")
    historical.upgrade()
    from app.db.migration_adoption import _require_0032_readiness_admissions_schema
    _require_0032_readiness_admissions_schema(connection)
    if _business(connection) != before:
        raise RuntimeError("0033 downgrade business catalog changed")