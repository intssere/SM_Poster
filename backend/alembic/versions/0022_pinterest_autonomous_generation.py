"""Autonomous portfolio generation run foundation.

Revision ID: 0022
Revises: 0021
"""
from alembic import op
import sqlalchemy as sa

revision = "0022"
down_revision = "0021"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "pinterest_autonomous_generation_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "portfolio_item_id",
            sa.String(36),
            sa.ForeignKey("pinterest_portfolio_plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column(
            "seo_brief_id",
            sa.String(36),
            sa.ForeignKey("pinterest_seo_briefs.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="STARTED"),
        sa.Column(
            "concept_id",
            sa.String(36),
            sa.ForeignKey("pin_concepts.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "draft_id",
            sa.String(36),
            sa.ForeignKey("pin_drafts.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "creative_id",
            sa.String(36),
            sa.ForeignKey("pin_creatives.id", ondelete="SET NULL"),
        ),
        sa.Column("safe_metadata", sa.JSON(), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True)),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "portfolio_item_id",
            name="uq_pinterest_autonomous_generation_item",
        ),
        sa.UniqueConstraint(
            "input_fingerprint",
            name="uq_pinterest_autonomous_generation_input_fp",
        ),
        sa.CheckConstraint(
            "status IN ('STARTED','SUCCEEDED','FAILED')",
            name="ck_pinterest_autonomous_generation_run_status",
        ),
    )
    op.create_index(
        "ix_pinterest_generation_runs_portfolio_item",
        "pinterest_autonomous_generation_runs",
        ["portfolio_item_id"],
    )
    op.create_index(
        "ix_pinterest_generation_runs_seo_brief",
        "pinterest_autonomous_generation_runs",
        ["seo_brief_id"],
    )
    op.create_index(
        "ix_pinterest_generation_runs_status",
        "pinterest_autonomous_generation_runs",
        ["status"],
    )
    op.create_index(
        "ix_pinterest_generation_runs_concept",
        "pinterest_autonomous_generation_runs",
        ["concept_id"],
    )
    op.create_index(
        "ix_pinterest_generation_runs_draft",
        "pinterest_autonomous_generation_runs",
        ["draft_id"],
    )
    op.create_index(
        "ix_pinterest_generation_runs_creative",
        "pinterest_autonomous_generation_runs",
        ["creative_id"],
    )


def downgrade():
    op.drop_table("pinterest_autonomous_generation_runs")
