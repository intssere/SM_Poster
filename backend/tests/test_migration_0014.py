import importlib.util
from pathlib import Path
import sqlalchemy as sa
from alembic.migration import MigrationContext
from alembic.operations import Operations
from sqlalchemy.exc import IntegrityError

ROOT = Path(__file__).resolve().parents[1]

def test_0013_to_0014_preserves_history_and_adds_attempt_audit():
    engine = sa.create_engine("sqlite+pysqlite:///:memory:")
    meta = sa.MetaData()
    sa.Table("boards", meta, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table("pinterest_connections", meta, sa.Column("id", sa.String(36), primary_key=True))
    sa.Table("pinterest_boards", meta, sa.Column("id", sa.String(36), primary_key=True), sa.Column("connection_id", sa.String(36)))
    sa.Table("pin_publications", meta, sa.Column("id", sa.String(36), primary_key=True), sa.Column("board_id", sa.String(36), sa.ForeignKey("boards.id"), nullable=False), sa.Column("approval_id", sa.String(36)))
    sa.Table("pin_approvals", meta, sa.Column("id", sa.String(36), primary_key=True))
    with engine.begin() as conn:
        meta.create_all(conn)
        conn.execute(sa.text("INSERT INTO pin_approvals(id) VALUES ('a1')"))
        conn.execute(sa.text("INSERT INTO boards(id) VALUES ('legacy-b')"))
        conn.execute(sa.text("INSERT INTO pinterest_connections(id) VALUES ('c1')"))
        conn.execute(sa.text("INSERT INTO pinterest_boards(id, connection_id) VALUES ('b1','c1')"))
        conn.execute(sa.text("INSERT INTO pin_publications(id, board_id, approval_id) VALUES ('p1','legacy-b','a1')"))
        ctx = MigrationContext.configure(conn)
        spec = importlib.util.spec_from_file_location("migration_0014", ROOT / "alembic/versions/0014_publisher_scheduler_foundation.py")
        migration = importlib.util.module_from_spec(spec); spec.loader.exec_module(migration)
        migration.op = Operations(ctx); migration.upgrade()
        row = conn.execute(sa.text("SELECT id, board_id, approval_id, pinterest_connection_id, pinterest_board_record_id, pinterest_board_id_snapshot, title_snapshot, description_snapshot, alt_text_snapshot, media_url_snapshot FROM pin_publications WHERE id='p1'")).one()
        assert row == ("p1", "legacy-b", "a1", None, None, None, None, None, None, None)
        insp = sa.inspect(conn)
        assert "publication_attempts" in insp.get_table_names()
        board_col = next(c for c in insp.get_columns("pin_publications") if c["name"] == "board_id")
        assert board_col["nullable"] is True
        conn.execute(sa.text("INSERT INTO pin_publications(id, board_id, approval_id, pinterest_connection_id, pinterest_board_record_id, pinterest_board_id_snapshot) VALUES ('p2',NULL,'a1','c1','b1','ext-1')"))
        assert conn.scalar(sa.text("SELECT COUNT(*) FROM publication_attempts WHERE publication_id='p1'")) == 0
        conn.execute(sa.text("INSERT INTO publication_attempts(id, publication_id, attempt_number, status, safe_response_metadata) VALUES ('x1','p1',1,'STARTED','{}')"))
        try:
            conn.execute(sa.text("INSERT INTO publication_attempts(id, publication_id, attempt_number, status, safe_response_metadata) VALUES ('x2','p1',1,'STARTED','{}')"))
            raise AssertionError("duplicate attempt number accepted")
        except IntegrityError:
            pass
        conn.execute(sa.text("INSERT INTO publication_attempts(id, publication_id, attempt_number, status, safe_response_metadata) VALUES ('x3','p1',2,'STARTED','{}')"))
