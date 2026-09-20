"""Autonomous monthly Pinterest portfolio plan foundation.

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pinterest_portfolio_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "store_id",
            sa.String(36),
            sa.ForeignKey("stores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("month_key", sa.String(7), nullable=False),
        sa.Column("timezone", sa.String(80), nullable=False),
        sa.Column("target_count", sa.Integer(), nullable=False),
        sa.Column("existing_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("planned_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserve_count", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("plan_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("summary", sa.JSON(), nullable=False),
        sa.Column("generated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "store_id",
            "month_key",
            name="uq_pinterest_portfolio_store_month",
        ),
        sa.UniqueConstraint(
            "plan_fingerprint",
            name="uq_pinterest_portfolio_plan_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN ('DRAFT','ACTIVE','COMPLETED','CANCELLED')",
            name="ck_pinterest_portfolio_plan_status",
        ),
    )
    op.create_index(
        "ix_pinterest_portfolio_plans_store_id",
        "pinterest_portfolio_plans",
        ["store_id"],
    )
    op.create_index(
        "ix_pinterest_portfolio_plans_month_key",
        "pinterest_portfolio_plans",
        ["month_key"],
    )

    op.create_table(
        "pinterest_portfolio_slots",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("pinterest_portfolio_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("sequence_no", sa.Integer(), nullable=False),
        sa.Column("slot_kind", sa.String(20), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True)),
        sa.Column(
            "product_id",
            sa.String(36),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "board_id",
            sa.String(36),
            sa.ForeignKey("boards.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "content_angle_id",
            sa.String(36),
            sa.ForeignKey("content_angles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "keyword_cluster_id",
            sa.String(36),
            sa.ForeignKey("keyword_clusters.id", ondelete="RESTRICT"),
        ),
        sa.Column("candidate_fingerprint", sa.String(64), nullable=False),
        sa.Column("slot_fingerprint", sa.String(64), nullable=False),
        sa.Column("brand_key", sa.String(255)),
        sa.Column("rationale", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "plan_id",
            "sequence_no",
            name="uq_pinterest_portfolio_slot_sequence",
        ),
        sa.UniqueConstraint(
            "plan_id",
            "candidate_fingerprint",
            name="uq_pinterest_portfolio_candidate",
        ),
        sa.UniqueConstraint(
            "slot_fingerprint",
            name="uq_pinterest_portfolio_slot_fingerprint",
        ),
        sa.CheckConstraint(
            "slot_kind IN ('PLANNED','RESERVE')",
            name="ck_pinterest_portfolio_slot_kind",
        ),
    )
    op.create_index(
        "ix_pinterest_portfolio_slots_plan_id",
        "pinterest_portfolio_slots",
        ["plan_id"],
    )
    op.create_index(
        "ix_pinterest_portfolio_slots_scheduled_for",
        "pinterest_portfolio_slots",
        ["scheduled_for"],
    )
    op.create_index(
        "ix_pinterest_portfolio_slots_product_id",
        "pinterest_portfolio_slots",
        ["product_id"],
    )
    op.create_index(
        "ix_pinterest_portfolio_slots_board_id",
        "pinterest_portfolio_slots",
        ["board_id"],
    )
    op.create_index(
        "ix_pinterest_portfolio_slots_angle_id",
        "pinterest_portfolio_slots",
        ["content_angle_id"],
    )
    op.create_index(
        "ix_pinterest_portfolio_slots_keyword_id",
        "pinterest_portfolio_slots",
        ["keyword_cluster_id"],
    )


def downgrade():
    op.drop_table("pinterest_portfolio_slots")
    op.drop_table("pinterest_portfolio_plans")
