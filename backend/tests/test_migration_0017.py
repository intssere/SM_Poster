"""Structural and round-trip tests for the durable Buffer activation migration."""
import importlib.util
from pathlib import Path

import pytest
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

from app.core.config import get_settings


ROOT = Path(__file__).resolve().parents[1]


def test_0017_upgrade_downgrade_and_active_partial_unique_constraint(tmp_path, monkeypatch):
    url = "sqlite+pysqlite:///" + (tmp_path / "migration-0017.db").as_posix()
    engine = sa.create_engine(url)
    try:
        with engine.begin() as conn:
            meta = sa.MetaData()
            sa.Table("pin_approvals", meta, sa.Column("id", sa.String(36), primary_key=True))
            sa.Table("pin_publications", meta, sa.Column("id", sa.String(36), primary_key=True))
            sa.Table("pinterest_boards", meta, sa.Column("id", sa.String(36), primary_key=True))
            sa.Table(
                "publication_attempts", meta,
                sa.Column("id", sa.String(36), primary_key=True),
                sa.Column("publication_id", sa.String(36), nullable=False),
            )
            meta.create_all(conn)
            spec = importlib.util.spec_from_file_location(
                "migration_0017", ROOT / "alembic/versions/0017_buffer_pilot_activation.py"
            )
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            module.op = Operations(MigrationContext.configure(conn))
            module.upgrade()
            columns = {c["name"] for c in sa.inspect(conn).get_columns("buffer_pilot_activations")}
            assert {"approval_id", "publication_id", "pinterest_board_record_id",
                    "publication_fingerprint", "request_fingerprint", "actor",
                    "status"} <= columns
            indexes = {i["name"] for i in sa.inspect(conn).get_indexes("buffer_pilot_activations")}
            assert "uq_buffer_pilot_activation_active" in indexes
            attempt_columns = {c["name"] for c in sa.inspect(conn).get_columns("publication_attempts")}
            assert "buffer_pilot_activation_id" in attempt_columns
            module.op = Operations(MigrationContext.configure(conn))
            module.downgrade()
            assert "buffer_pilot_activations" not in sa.inspect(conn).get_table_names()
            assert "buffer_pilot_activation_id" not in {
                c["name"] for c in sa.inspect(conn).get_columns("publication_attempts")
            }
    finally:
        engine.dispose()


def _migration_module():
    spec = importlib.util.spec_from_file_location(
        "migration_0017_extra", ROOT / "alembic/versions/0017_buffer_pilot_activation.py"
    )
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _prerequisites(conn):
    meta = sa.MetaData()
    for name in ("pin_approvals", "pin_publications", "pinterest_boards"):
        sa.Table(name, meta, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table("publication_attempts", meta, sa.Column("id", sa.String(36), primary_key=True))
    meta.create_all(conn)


def _activation_table(conn, *, activated_type=sa.DateTime(timezone=True),
                      optional_timestamps=True, activated_default=sa.func.now()):
    meta = sa.MetaData()
    sa.Table(
        "buffer_pilot_activations", meta,
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("approval_id", sa.String(36), nullable=False),
        sa.Column("publication_id", sa.String(36), nullable=False),
        sa.Column("pinterest_board_record_id", sa.String(36), nullable=False),
        sa.Column("publication_fingerprint", sa.String(64), nullable=False),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("actor", sa.String(255), nullable=False),
        sa.Column("activated_at", activated_type, nullable=False, server_default=activated_default),
        sa.Column("status", sa.String(20), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=optional_timestamps),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=optional_timestamps),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=optional_timestamps),
        sa.Column("revoked_by", sa.String(255)),
        sa.Column("revoke_reason", sa.String(255)),
    ).create(conn)


@pytest.mark.parametrize("activated_type", [sa.Text(), sa.Integer()])
def test_0017_adoption_rejects_wrong_activated_at_type(tmp_path, activated_type):
    engine = sa.create_engine("sqlite+pysqlite:///" + (tmp_path / "bad-type.db").as_posix())
    with engine.begin() as conn:
        _prerequisites(conn)
        _activation_table(conn, activated_type=activated_type)
        module = _migration_module()
        module.op = Operations(MigrationContext.configure(conn))
        with pytest.raises(RuntimeError, match="contract"):
            module.upgrade()


def test_0017_adoption_rejects_required_timestamp_as_optional(tmp_path):
    engine = sa.create_engine("sqlite+pysqlite:///" + (tmp_path / "bad-null.db").as_posix())
    with engine.begin() as conn:
        _prerequisites(conn)
        _activation_table(conn, optional_timestamps=False)
        module = _migration_module()
        module.op = Operations(MigrationContext.configure(conn))
        with pytest.raises(RuntimeError, match="schema contract"):
            module.upgrade()


def test_0017_adoption_rejects_non_current_time_default(tmp_path):
    engine = sa.create_engine("sqlite+pysqlite:///" + (tmp_path / "bad-default.db").as_posix())
    with engine.begin() as conn:
        _prerequisites(conn)
        _activation_table(conn, activated_default=sa.text("'2000-01-01 00:00:00'"))
        module = _migration_module()
        module.op = Operations(MigrationContext.configure(conn))
        with pytest.raises(RuntimeError, match="default contract"):
            module.upgrade()


@pytest.mark.parametrize("predicate", [
    "status != 'ACTIVE'", "status = 'INACTIVE'", "status = 'ACTIVE' OR 1=1",
])
def test_0017_rejects_adversarial_partial_predicates(tmp_path, predicate):
    engine = sa.create_engine("sqlite+pysqlite:///" + (tmp_path / "bad-predicate.db").as_posix())
    with engine.begin() as conn:
        _prerequisites(conn)
        module = _migration_module()
        module.op = Operations(MigrationContext.configure(conn))
        module.upgrade()
        sa.inspect(conn).clear_cache() if hasattr(sa.inspect(conn), "clear_cache") else None
        conn.exec_driver_sql("DROP INDEX uq_buffer_pilot_activation_active")
        conn.exec_driver_sql(
            f"CREATE UNIQUE INDEX uq_buffer_pilot_activation_active "
            f"ON buffer_pilot_activations(status) WHERE {predicate}"
        )
        module.op = Operations(MigrationContext.configure(conn))
        with pytest.raises(RuntimeError, match="partial-index predicate"):
            module.upgrade()


def test_0017_index_predicate_unit_postgresql_shape_is_fail_closed():
    module = _migration_module()
    class FakeBind:
        dialect = type("Dialect", (), {"name": "postgresql"})()
    for predicate in ("status != 'ACTIVE'", "status = 'ACTIVE' OR 1=1"):
        module.sa.inspect = lambda bind, predicate=predicate: type(
            "Inspector", (), {"get_indexes": lambda self, table: [{
                "name": "uq_buffer_pilot_activation_active", "column_names": ["status"],
                "unique": True, "dialect_options": {"postgresql_where": predicate},
            }]}
        )()
        with pytest.raises(RuntimeError, match="partial-index predicate"):
            module._index_contract(FakeBind(), "buffer_pilot_activations",
                                   "uq_buffer_pilot_activation_active", ["status"],
                                   unique=True, predicate=True)