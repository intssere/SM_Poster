"""Pinterest SEO brief foundation.

Revision ID: 0021
Revises: 0020
"""
from alembic import op
import sqlalchemy as sa

from app.db.preapplied_schema_adoption import adopt_preapplied_revision

revision = "0021"
down_revision = "0020"
branch_labels = None
depends_on = None


def upgrade():
    if adopt_preapplied_revision(op.get_bind(), "0021"):
        return
    op.create_table(
        "pinterest_seo_briefs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "portfolio_item_id",
            sa.String(36),
            sa.ForeignKey("pinterest_portfolio_plan_items.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("seo_fingerprint", sa.String(64), nullable=False),
        sa.Column("primary_keyword", sa.String(255), nullable=False),
        sa.Column("secondary_keywords", sa.JSON(), nullable=False),
        sa.Column("intent", sa.String(100), nullable=False),
        sa.Column("source_evidence", sa.JSON(), nullable=False),
        sa.Column("dimension_scores", sa.JSON(), nullable=False),
        sa.Column("coverage_targets", sa.JSON(), nullable=False),
        sa.Column("guidance", sa.JSON(), nullable=False),
        sa.Column("cannibalization_warnings", sa.JSON(), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="CURRENT"),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "portfolio_item_id",
            name="uq_pinterest_seo_brief_portfolio_item",
        ),
        sa.UniqueConstraint(
            "seo_fingerprint",
            name="uq_pinterest_seo_brief_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN ('CURRENT','SUPERSEDED')",
            name="ck_pinterest_seo_brief_status",
        ),
    )
    op.create_index(
        "ix_pinterest_seo_briefs_portfolio_item_id",
        "pinterest_seo_briefs",
        ["portfolio_item_id"],
    )
    op.create_index(
        "ix_pinterest_seo_briefs_input_fingerprint",
        "pinterest_seo_briefs",
        ["input_fingerprint"],
    )
    op.create_index(
        "ix_pinterest_seo_briefs_status",
        "pinterest_seo_briefs",
        ["status"],
    )


def downgrade():
    op.drop_table("pinterest_seo_briefs")
