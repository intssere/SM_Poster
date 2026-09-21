"""Autonomous monthly Pinterest portfolio planner foundation.

Revision ID: 0020
Revises: 0019
"""
from alembic import op
import sqlalchemy as sa
from app.db.migration_adoption import (
    adopt_preapplied_revision,
    repair_known_legacy_preapplied_revision,
)

revision = "0020"
down_revision = "0019"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    repaired_legacy = repair_known_legacy_preapplied_revision(bind, revision)
    if adopt_preapplied_revision(bind, revision):
        return
    op.create_table(
        "pinterest_portfolio_plans",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "store_id",
            sa.String(36),
            sa.ForeignKey("stores.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column("month_end", sa.Date(), nullable=False),
        sa.Column("target_pins", sa.Integer(), nullable=False),
        sa.Column("existing_commitments", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("planned_active_slots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("reserve_slots", sa.Integer(), nullable=False, server_default="0"),
        sa.Column("policy_version", sa.String(80), nullable=False),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("plan_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="DRAFT"),
        sa.Column("metadata_json", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
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
        "ix_pinterest_portfolio_plans_month_start",
        "pinterest_portfolio_plans",
        ["month_start"],
    )
    op.create_index(
        "ix_pinterest_portfolio_plans_input_fp",
        "pinterest_portfolio_plans",
        ["input_fingerprint"],
    )
    op.create_index(
        "ix_pinterest_portfolio_plans_status",
        "pinterest_portfolio_plans",
        ["status"],
    )
    op.create_index(
        "uq_pinterest_portfolio_active_month",
        "pinterest_portfolio_plans",
        ["store_id", "month_start"],
        unique=True,
        postgresql_where=sa.text("status IN ('DRAFT','ACTIVE')"),
        sqlite_where=sa.text("status IN ('DRAFT','ACTIVE')"),
    )

    op.create_table(
        "pinterest_portfolio_plan_items",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("pinterest_portfolio_plans.id", ondelete="CASCADE"),
            nullable=False,
        ),
        sa.Column("slot_index", sa.Integer(), nullable=False),
        sa.Column("is_reserve", sa.Boolean(), nullable=False, server_default=sa.false()),
        sa.Column("planned_date", sa.Date()),
        sa.Column(
            "product_id",
            sa.String(36),
            sa.ForeignKey("products.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "local_board_id",
            sa.String(36),
            sa.ForeignKey("boards.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("board_key_snapshot", sa.String(255), nullable=False),
        sa.Column(
            "content_angle_id",
            sa.String(36),
            sa.ForeignKey("content_angles.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("angle_key_snapshot", sa.String(100), nullable=False),
        sa.Column("seed_keywords", sa.JSON(), nullable=False),
        sa.Column("selection_score", sa.Numeric(12, 6), nullable=False),
        sa.Column("selection_metadata", sa.JSON(), nullable=False),
        sa.Column("item_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="PLANNED"),
        sa.Column(
            "publication_id",
            sa.String(36),
            sa.ForeignKey("pin_publications.id", ondelete="SET NULL"),
        ),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.Column(
            "updated_at",
            sa.DateTime(timezone=True),
            nullable=False,
            server_default=sa.func.now(),
        ),
        sa.UniqueConstraint(
            "plan_id",
            "slot_index",
            name="uq_pinterest_portfolio_plan_item_slot",
        ),
        sa.UniqueConstraint(
            "item_fingerprint",
            name="uq_pinterest_portfolio_item_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN ('PLANNED','PROMOTED','GENERATED','SCHEDULED','PUBLISHED','FAILED','SKIPPED')",
            name="ck_pinterest_portfolio_plan_item_status",
        ),
    )
    op.create_index(
        "ix_pinterest_portfolio_items_plan_id",
        "pinterest_portfolio_plan_items",
        ["plan_id"],
    )
    op.create_index(
        "ix_pinterest_portfolio_items_planned_date",
        "pinterest_portfolio_plan_items",
        ["planned_date"],
    )
    op.create_index(
        "ix_pinterest_portfolio_items_product_id",
        "pinterest_portfolio_plan_items",
        ["product_id"],
    )
    op.create_index(
        "ix_pinterest_portfolio_items_board_id",
        "pinterest_portfolio_plan_items",
        ["local_board_id"],
    )
    op.create_index(
        "ix_pinterest_portfolio_items_angle_id",
        "pinterest_portfolio_plan_items",
        ["content_angle_id"],
    )
    op.create_index(
        "ix_pinterest_portfolio_items_status",
        "pinterest_portfolio_plan_items",
        ["status"],
    )
    op.create_index(
        "ix_pinterest_portfolio_items_publication_id",
        "pinterest_portfolio_plan_items",
        ["publication_id"],
    )

    # A repaired legacy pair must end this same transaction as the exact frozen
    # canonical 0020 contract. Any mismatch aborts and restores the legacy
    # tables because PostgreSQL DDL is transactional.
    if repaired_legacy and not adopt_preapplied_revision(bind, revision):
        raise RuntimeError(
            "revision 0020 legacy repair did not recreate canonical tables"
        )


def downgrade():
    op.drop_table("pinterest_portfolio_plan_items")
    op.drop_table("pinterest_portfolio_plans")
