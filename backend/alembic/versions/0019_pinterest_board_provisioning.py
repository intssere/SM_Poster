"""Pinterest board provisioning attempt boundary.

Revision ID: 0019
Revises: 0018
"""
from alembic import op
import sqlalchemy as sa

revision = "0019"
down_revision = "0018"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pinterest_board_provisioning_attempts",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "connection_id",
            sa.String(36),
            sa.ForeignKey("pinterest_connections.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("canonical_key", sa.String(120), nullable=False),
        sa.Column("desired_name", sa.String(255), nullable=False),
        sa.Column("desired_description", sa.Text(), nullable=False),
        sa.Column("privacy", sa.String(40), nullable=False, server_default="PUBLIC"),
        sa.Column("request_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="STARTED"),
        sa.Column("provider_board_id", sa.String(255)),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("provider_mutation_started_at", sa.DateTime(timezone=True)),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column("error_code", sa.String(120)),
        sa.Column("safe_metadata", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.CheckConstraint(
            "status IN ('STARTED','SUCCEEDED','FAILED','UNKNOWN')",
            name="ck_pinterest_board_provisioning_attempt_status",
        ),
        sa.UniqueConstraint(
            "request_fingerprint",
            name="uq_pinterest_board_provisioning_request_fingerprint",
        ),
    )
    op.create_index(
        "ix_pinterest_board_provisioning_attempts_connection_id",
        "pinterest_board_provisioning_attempts",
        ["connection_id"],
    )
    op.create_index(
        "ix_pinterest_board_provisioning_attempts_canonical_key",
        "pinterest_board_provisioning_attempts",
        ["canonical_key"],
    )
    op.create_index(
        "ix_pinterest_board_provisioning_attempts_status",
        "pinterest_board_provisioning_attempts",
        ["status"],
    )
    op.create_index(
        "ix_pinterest_board_provisioning_attempts_provider_mutation_started_at",
        "pinterest_board_provisioning_attempts",
        ["provider_mutation_started_at"],
    )
    op.create_index(
        "ix_pinterest_board_provisioning_attempts_provider_board_id",
        "pinterest_board_provisioning_attempts",
        ["provider_board_id"],
    )
    op.create_index(
        "ix_pinterest_board_provisioning_connection_key",
        "pinterest_board_provisioning_attempts",
        ["connection_id", "canonical_key"],
    )


def downgrade():
    op.drop_table("pinterest_board_provisioning_attempts")
