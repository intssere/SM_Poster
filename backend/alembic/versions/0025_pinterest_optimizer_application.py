"""Autonomous portfolio activation and adaptive optimizer application audit.

Revision ID: 0025
Revises: 0024
"""
from alembic import op
import sqlalchemy as sa

from app.db.preapplied_schema_adoption import adopt_preapplied_revision

revision = "0025"
down_revision = "0024"
branch_labels = None
depends_on = None


def upgrade():
    if adopt_preapplied_revision(op.get_bind(), "0025"):
        return
    op.create_table(
        "pinterest_optimizer_applications",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("pinterest_portfolio_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("plan_fingerprint_snapshot", sa.String(64), nullable=False),
        sa.Column("optimizer_policy_version", sa.String(80), nullable=False),
        sa.Column("optimizer_fingerprint", sa.String(64), nullable=False),
        sa.Column("learning_fingerprint", sa.String(64)),
        sa.Column("input_state_fingerprint", sa.String(64), nullable=False),
        sa.Column("frozen_item_count", sa.Integer(), nullable=False),
        sa.Column("optimizable_item_count", sa.Integer(), nullable=False),
        sa.Column("exploit_count", sa.Integer(), nullable=False),
        sa.Column("explore_count", sa.Integer(), nullable=False),
        sa.Column("recommendation_snapshot", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="APPLIED"),
        sa.Column("applied_by", sa.String(255), nullable=False),
        sa.Column("applied_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint("plan_id", name="uq_pinterest_optimizer_application_plan"),
        sa.UniqueConstraint(
            "optimizer_fingerprint",
            name="uq_pinterest_optimizer_application_fingerprint",
        ),
        sa.CheckConstraint(
            "status = 'APPLIED'",
            name="ck_pinterest_optimizer_application_status",
        ),
        sa.CheckConstraint(
            "frozen_item_count >= 0 AND optimizable_item_count >= 0 "
            "AND exploit_count >= 0 AND explore_count >= 0",
            name="ck_pinterest_optimizer_application_counts",
        ),
    )
    op.create_index(
        "ix_pinterest_optimizer_applications_plan_id",
        "pinterest_optimizer_applications",
        ["plan_id"],
    )
    op.create_index(
        "ix_pinterest_optimizer_applications_input_state",
        "pinterest_optimizer_applications",
        ["input_state_fingerprint"],
    )
    op.create_index(
        "ix_pinterest_optimizer_applications_status",
        "pinterest_optimizer_applications",
        ["status"],
    )


def downgrade():
    op.drop_table("pinterest_optimizer_applications")
