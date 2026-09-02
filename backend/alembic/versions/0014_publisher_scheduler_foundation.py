"""publisher scheduler foundation audit fields

Revision ID: 0014
Revises: 0013
"""
from alembic import op
import sqlalchemy as sa

revision = "0014"
down_revision = "0013"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column("pin_publications", sa.Column("pinterest_connection_id", sa.String(36), nullable=True))
    op.add_column("pin_publications", sa.Column("pinterest_board_record_id", sa.String(36), nullable=True))
    op.add_column("pin_publications", sa.Column("pinterest_board_id_snapshot", sa.String(255), nullable=True))
    op.add_column("pin_publications", sa.Column("title_snapshot", sa.Text(), nullable=True))
    op.add_column("pin_publications", sa.Column("description_snapshot", sa.Text(), nullable=True))
    op.add_column("pin_publications", sa.Column("alt_text_snapshot", sa.Text(), nullable=True))
    op.add_column("pin_publications", sa.Column("media_url_snapshot", sa.Text(), nullable=True))
    with op.batch_alter_table("pin_publications") as batch:
        batch.create_foreign_key("fk_pin_publications_pinterest_connection", "pinterest_connections", ["pinterest_connection_id"], ["id"], ondelete="RESTRICT")
        batch.create_foreign_key("fk_pin_publications_pinterest_board_record", "pinterest_boards", ["pinterest_board_record_id"], ["id"], ondelete="RESTRICT")
    op.create_index("ix_pin_publications_pinterest_connection_id", "pin_publications", ["pinterest_connection_id"])
    op.create_index("ix_pin_publications_pinterest_board_record_id", "pin_publications", ["pinterest_board_record_id"])
    op.create_table(
        "publication_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column("publication_id", sa.String(36), sa.ForeignKey("pin_publications.id", ondelete="CASCADE"), nullable=False),
        sa.Column("attempt_number", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(30), nullable=False),
        sa.Column("request_fingerprint", sa.String(64)),
        sa.Column("started_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("provider_pin_id", sa.String(255)),
        sa.Column("error_code", sa.String(100)),
        sa.Column("safe_response_metadata", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), server_default=sa.func.now()),
        sa.UniqueConstraint("publication_id", "attempt_number", name="uq_publication_attempt_number"),
    )
    op.create_index("ix_publication_attempts_publication_id", "publication_attempts", ["publication_id"])


def downgrade():
    op.drop_index("ix_publication_attempts_publication_id", table_name="publication_attempts")
    op.drop_table("publication_attempts")
    op.drop_index("ix_pin_publications_pinterest_board_record_id", table_name="pin_publications")
    op.drop_index("ix_pin_publications_pinterest_connection_id", table_name="pin_publications")
    with op.batch_alter_table("pin_publications") as batch:
        batch.drop_constraint("fk_pin_publications_pinterest_board_record", type_="foreignkey")
        batch.drop_constraint("fk_pin_publications_pinterest_connection", type_="foreignkey")
        for name in ("media_url_snapshot", "alt_text_snapshot", "description_snapshot", "title_snapshot", "pinterest_board_id_snapshot", "pinterest_board_record_id", "pinterest_connection_id"):
            batch.drop_column(name)
