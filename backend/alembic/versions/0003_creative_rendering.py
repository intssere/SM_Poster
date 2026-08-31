"""creative rendering metadata

Revision ID: 0003
Revises: 0002
"""

from alembic import op
import sqlalchemy as sa

revision = "0003"
down_revision = "0002"
branch_labels = None
depends_on = None


def upgrade():
    op.alter_column("pin_creatives", "rendered_url", existing_type=sa.Text(), nullable=True)
    op.alter_column("pin_creatives", "sha256", existing_type=sa.String(length=64), nullable=True)
    op.add_column("pin_creatives", sa.Column("render_status", sa.String(length=30), nullable=False, server_default="PENDING"))
    op.add_column("pin_creatives", sa.Column("render_error", sa.Text(), nullable=True))
    op.add_column("pin_creatives", sa.Column("render_spec", sa.JSON(), nullable=False, server_default="{}"))
    op.add_column("pin_creatives", sa.Column("rendered_at", sa.DateTime(timezone=True), nullable=True))
    op.add_column("pin_creatives", sa.Column("render_duration_ms", sa.Integer(), nullable=True))
    op.add_column("pin_creatives", sa.Column("size_bytes", sa.Integer(), nullable=True))
    op.create_index("ix_pin_creatives_render_status", "pin_creatives", ["render_status"])


def downgrade():
    op.drop_index("ix_pin_creatives_render_status", table_name="pin_creatives")
    # Pre-render and failed rows did not exist in the prior schema and cannot
    # satisfy its required output URL/checksum contract.
    op.execute("DELETE FROM pin_creatives WHERE rendered_url IS NULL OR sha256 IS NULL")
    op.drop_column("pin_creatives", "size_bytes")
    op.drop_column("pin_creatives", "render_duration_ms")
    op.drop_column("pin_creatives", "rendered_at")
    op.drop_column("pin_creatives", "render_spec")
    op.drop_column("pin_creatives", "render_error")
    op.drop_column("pin_creatives", "render_status")
    op.alter_column("pin_creatives", "sha256", existing_type=sa.String(length=64), nullable=False)
    op.alter_column("pin_creatives", "rendered_url", existing_type=sa.Text(), nullable=False)