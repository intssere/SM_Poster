"""Pinterest deterministic learning/ranking snapshots.

Revision ID: 0024
Revises: 0023
"""
from alembic import op
import sqlalchemy as sa
from app.db.migration_adoption import adopt_preapplied_revision

revision = "0024"
down_revision = "0023"
branch_labels = None
depends_on = None


def upgrade():
    if adopt_preapplied_revision(op.get_bind(), revision):
        return
    op.create_table(
        "pinterest_learning_snapshots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "store_id",
            sa.String(36),
            sa.ForeignKey("stores.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("as_of_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("learning_fingerprint", sa.String(64), nullable=False),
        sa.Column("publication_count", sa.Integer(), nullable=False),
        sa.Column("snapshot_count", sa.Integer(), nullable=False),
        sa.Column("global_priors", sa.JSON(), nullable=False),
        sa.Column("rankings", sa.JSON(), nullable=False),
        sa.Column("optimizer_ready", sa.Boolean(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "store_id",
            "policy_version",
            "input_fingerprint",
            name="uq_pinterest_learning_input",
        ),
        sa.UniqueConstraint(
            "learning_fingerprint",
            name="uq_pinterest_learning_fingerprint",
        ),
        sa.CheckConstraint(
            "publication_count >= 0 AND snapshot_count >= 0",
            name="ck_pinterest_learning_counts_nonnegative",
        ),
    )
    op.create_index(
        "ix_pinterest_learning_snapshots_store_id",
        "pinterest_learning_snapshots",
        ["store_id"],
    )
    op.create_index(
        "ix_pinterest_learning_snapshots_as_of_at",
        "pinterest_learning_snapshots",
        ["as_of_at"],
    )
    op.create_index(
        "ix_pinterest_learning_snapshots_input_fp",
        "pinterest_learning_snapshots",
        ["input_fingerprint"],
    )


def downgrade():
    op.drop_table("pinterest_learning_snapshots")
