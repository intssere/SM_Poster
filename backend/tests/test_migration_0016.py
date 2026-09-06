"""Narrow persisted 0015 schema, real Alembic upgrade/downgrade/re-upgrade."""
import importlib.util
from pathlib import Path
import sqlalchemy as sa
import pytest
from alembic import command
from alembic.config import Config
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError
from app.core.config import get_settings

ROOT = Path(__file__).resolve().parents[1]


def migration(number, name, conn):
    spec = importlib.util.spec_from_file_location("migration_" + number, ROOT / "alembic/versions" / name)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    module.op = Operations(MigrationContext.configure(conn))
    module.upgrade()


def test_0015_0016_preservation_uniqueness_and_roundtrip(tmp_path, monkeypatch):
    url = "sqlite+pysqlite:///" + (tmp_path / "isolated-migration.db").as_posix()
    engine = sa.create_engine(url)
    meta = sa.MetaData()
    for name in ("boards", "pinterest_connections", "pinterest_boards"):
        sa.Table(name, meta, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table("pin_publications", meta, sa.Column("id", sa.String(36), primary_key=True),
             sa.Column("board_id", sa.String(36), nullable=False))
    with engine.begin() as conn:
        meta.create_all(conn)
        migration("0014", "0014_publisher_scheduler_foundation.py", conn)
        migration("0015", "0015_manual_dispatch_readiness.py", conn)
        conn.execute(sa.text("INSERT INTO pin_publications(id, board_id) VALUES ('p', NULL)"))
        conn.execute(sa.text("INSERT INTO publication_attempts(id,publication_id,attempt_number,status,safe_response_metadata,provider_pin_id) VALUES ('a','p',1,'SUCCEEDED','{}','123')"))
        conn.execute(sa.text("INSERT INTO publication_reconciliation_events(id,publication_id,attempt_id,actor,action,previous_status,new_status,provider_pin_id) VALUES ('e','p','a','operator','PROVIDER_PIN_CONFIRMED','PUBLISH_UNKNOWN','PUBLISHED','123')"))
        before = {name: dict(conn.execute(sa.text(f"SELECT * FROM {name}")).mappings().one()) for name in
                  ("pin_publications", "publication_attempts", "publication_reconciliation_events")}
        unrelated = {name: sa.inspect(conn).get_columns(name) for name in ("boards", "pinterest_connections", "pinterest_boards", "publication_dispatch_authorizations")}
    monkeypatch.setenv("DATABASE_URL", url)
    get_settings.cache_clear()
    cfg = Config(str(ROOT / "alembic.ini"))
    cfg.set_main_option("script_location", str(ROOT / "alembic"))
    cfg.set_main_option("path_separator", "os")
    try:
        command.stamp(cfg, "0015")
        with engine.connect() as conn:
            assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0015"
        command.upgrade(cfg, "0016")
        with engine.begin() as conn:
            assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0016"
            for name, row in before.items():
                after = dict(conn.execute(sa.text(f"SELECT * FROM {name}")).mappings().one())
                assert {key: after[key] for key in row} == row
            assert conn.scalar(sa.text("SELECT dispatch_provider FROM publication_attempts")) == "pinterest_direct"
            assert conn.scalar(sa.text("SELECT provider FROM publication_reconciliation_events")) == "pinterest_direct"
            columns = {c["name"] for c in sa.inspect(conn).get_columns("publication_attempts")}
            assert {"dispatch_provider", "provider_operation_id", "provider_operation_status", "provider_external_link", "provider_submitted_at", "provider_last_observed_at"} <= columns
            assert "uq_publication_attempt_provider_operation" in {i["name"] for i in sa.inspect(conn).get_indexes("publication_attempts")}
            for name, original in unrelated.items():
                current = sa.inspect(conn).get_columns(name)
                assert [(c["name"], str(c["type"]), c["nullable"]) for c in current] == [(c["name"], str(c["type"]), c["nullable"]) for c in original]
            # Use a savepoint for new Phase 2 evidence, then discard fixture rows before structural downgrade.
            nested = conn.begin_nested()
            for i in (2, 3):
                conn.execute(sa.text("INSERT INTO publication_attempts(id,publication_id,attempt_number,status,safe_response_metadata) VALUES (:id,'p',:n,'UNKNOWN','{}')"), {"id": str(i), "n": i})
            conn.execute(sa.text("UPDATE publication_attempts SET dispatch_provider='buffer',provider_operation_id='op' WHERE id='2'"))
            with pytest.raises(IntegrityError):
                conn.execute(sa.text("UPDATE publication_attempts SET dispatch_provider='buffer',provider_operation_id='op' WHERE id='3'"))
            conn.execute(sa.text("INSERT INTO publication_reconciliation_events(id,publication_id,actor,action,previous_status,new_status,provider) VALUES ('failure','p','operator','PROVIDER_FAILURE_CONFIRMED','PUBLISH_UNKNOWN','PUBLISH_FAILED','buffer')"))
            with pytest.raises(IntegrityError):
                conn.execute(sa.text("INSERT INTO publication_reconciliation_events(id,publication_id,actor,action,previous_status,new_status) VALUES ('invalid','p','operator','PROVIDER_FAILURE_CONFIRMED','PUBLISH_UNKNOWN','PUBLISHED')"))
            nested.rollback()
        command.downgrade(cfg, "0015")
        with engine.connect() as conn:
            assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0015"
            for name, row in before.items():
                assert dict(conn.execute(sa.text(f"SELECT * FROM {name}")).mappings().one()) == row
        command.upgrade(cfg, "0016")
        with engine.connect() as conn:
            assert conn.scalar(sa.text("SELECT version_num FROM alembic_version")) == "0016"
    finally:
        get_settings.cache_clear()
        engine.dispose()
