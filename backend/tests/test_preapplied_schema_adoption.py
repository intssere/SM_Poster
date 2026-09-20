import os

import pytest
import sqlalchemy as sa
from alembic import command
from alembic.config import Config
from alembic.operations import Operations
from alembic.runtime.migration import MigrationContext
from alembic.script import ScriptDirectory

from app.core.config import get_settings
from app.db.preapplied_schema_adoption import (
    CONTRACTS,
    PreappliedSchemaAdoptionError,
)


POSTGRES_URL = os.getenv("TASK58_POSTGRES_URL")
REVISIONS = ("0020", "0021", "0022", "0023", "0024", "0025", "0026")
PREVIOUS = {
    "0020": "0019",
    "0021": "0020",
    "0022": "0021",
    "0023": "0022",
    "0024": "0023",
    "0025": "0024",
    "0026": "0025",
}


pytestmark = pytest.mark.skipif(
    not POSTGRES_URL,
    reason="TASK58_POSTGRES_URL not configured",
)


def _config() -> Config:
    return Config("alembic.ini")


def _set_database_url():
    os.environ["DATABASE_URL"] = POSTGRES_URL
    get_settings.cache_clear()


def _reset_database(engine):
    with engine.connect().execution_options(isolation_level="AUTOCOMMIT") as connection:
        connection.execute(sa.text("DROP SCHEMA IF EXISTS public CASCADE"))
        connection.execute(sa.text("CREATE SCHEMA public"))


def _upgrade(revision: str):
    _set_database_url()
    command.upgrade(_config(), revision)


def _current_revision(engine) -> str | None:
    inspector = sa.inspect(engine)
    if "alembic_version" not in inspector.get_table_names():
        return None
    with engine.connect() as connection:
        rows = connection.execute(
            sa.text("SELECT version_num FROM alembic_version")
        ).fetchall()
    assert len(rows) == 1
    return str(rows[0][0])


def _preapply_revision(engine, revision: str):
    _set_database_url()
    script = ScriptDirectory.from_config(_config())
    module = script.get_revision(revision).module
    with engine.begin() as connection:
        context = MigrationContext.configure(connection)
        operations = Operations(context)
        original = module.op
        module.op = operations
        try:
            module.upgrade()
        finally:
            module.op = original


@pytest.fixture
def engine():
    engine = sa.create_engine(POSTGRES_URL, pool_pre_ping=True)
    _reset_database(engine)
    try:
        yield engine
    finally:
        _reset_database(engine)
        engine.dispose()
        get_settings.cache_clear()


def test_fresh_postgres_upgrade_through_0026(engine):
    _upgrade("head")
    assert _current_revision(engine) == "0026"
    tables = set(sa.inspect(engine).get_table_names())
    for revision in REVISIONS:
        assert set(CONTRACTS[revision]).issubset(tables)


def test_structural_0020_bookkeeping_0019_adopts_then_upgrades_to_0026(engine):
    _upgrade("0019")
    _preapply_revision(engine, "0020")

    assert _current_revision(engine) == "0019"
    with engine.connect() as connection:
        assert connection.execute(
            sa.text("SELECT count(*) FROM pinterest_portfolio_plans")
        ).scalar_one() == 0
        assert connection.execute(
            sa.text("SELECT count(*) FROM pinterest_portfolio_plan_items")
        ).scalar_one() == 0

    _upgrade("head")
    assert _current_revision(engine) == "0026"


def test_fully_preapplied_0020_through_0026_is_adopted_sequentially(engine):
    _upgrade("0019")
    for revision in REVISIONS:
        _preapply_revision(engine, revision)

    assert _current_revision(engine) == "0019"

    _upgrade("head")
    assert _current_revision(engine) == "0026"


@pytest.mark.parametrize(
    "revision,remaining_table,dropped_table",
    [
        (
            "0020",
            "pinterest_portfolio_plans",
            "pinterest_portfolio_plan_items",
        ),
        (
            "0023",
            "pinterest_analytics_snapshots",
            "pinterest_analytics_ingestion_runs",
        ),
    ],
)
def test_partial_multi_table_preapply_refuses(
    engine,
    revision,
    remaining_table,
    dropped_table,
):
    _upgrade(PREVIOUS[revision])
    _preapply_revision(engine, revision)
    with engine.begin() as connection:
        connection.execute(sa.text(f'DROP TABLE "{dropped_table}" CASCADE'))

    assert remaining_table in set(sa.inspect(engine).get_table_names())
    with pytest.raises(PreappliedSchemaAdoptionError, match="presence is partial"):
        _upgrade(revision)
    assert _current_revision(engine) == PREVIOUS[revision]


@pytest.mark.parametrize("revision", REVISIONS)
def test_structural_drift_refuses_before_bookkeeping_advances(engine, revision):
    previous = PREVIOUS[revision]
    _upgrade(previous)
    _preapply_revision(engine, revision)
    table = next(iter(CONTRACTS[revision]))

    with engine.begin() as connection:
        connection.execute(sa.text(f'ALTER TABLE "{table}" ADD COLUMN task58_drift TEXT'))

    with pytest.raises(PreappliedSchemaAdoptionError, match="column order/set differs"):
        _upgrade(revision)
    assert _current_revision(engine) == previous


def test_nonempty_preapplied_table_refuses_before_bookkeeping_advances(engine):
    _upgrade("0020")
    _preapply_revision(engine, "0021")

    with engine.begin() as connection:
        connection.execute(sa.text("SET session_replication_role = replica"))
        try:
            connection.execute(sa.text(
                """
                INSERT INTO pinterest_seo_briefs (
                    id, portfolio_item_id, policy_version,
                    input_fingerprint, seo_fingerprint,
                    primary_keyword, secondary_keywords, intent,
                    source_evidence, dimension_scores, coverage_targets,
                    guidance, cannibalization_warnings
                ) VALUES (
                    'brief-1', 'missing-item', 'v1',
                    :input_fp, :seo_fp,
                    'keyword', '[]'::json, 'commercial',
                    '{}'::json, '{}'::json, '{}'::json,
                    '{}'::json, '[]'::json
                )
                """
            ), {
                "input_fp": "a" * 64,
                "seo_fp": "b" * 64,
            })
        finally:
            connection.execute(sa.text("SET session_replication_role = origin"))

    with pytest.raises(PreappliedSchemaAdoptionError, match="is not empty"):
        _upgrade("0021")
    assert _current_revision(engine) == "0020"
