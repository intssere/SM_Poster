"""Add durable scheduled-autonomy quota reservations.

Revision ID: 0031
Revises: 0030
"""
from alembic import op
import sqlalchemy as sa


revision = "0031"
down_revision = "0030"
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        "routine_scheduled_quota_reservations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("publication_id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("plan_item_id", sa.String(length=36), nullable=False),
        sa.Column("product_id", sa.String(length=36), nullable=False),
        sa.Column("vendor_key", sa.String(length=255), nullable=False),
        sa.Column("board_id", sa.String(length=36), nullable=False),
        sa.Column("scheduled_for", sa.Date(), nullable=False),
        sa.Column("month_start", sa.Date(), nullable=False),
        sa.Column(
            "reserved_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.ForeignKeyConstraint(
            ["publication_id"],
            ["pin_publications.id"],
            name="fk_routine_scheduled_quota_publication",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["pinterest_portfolio_plans.id"],
            name="fk_routine_scheduled_quota_plan",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_item_id"],
            ["pinterest_portfolio_plan_items.id"],
            name="fk_routine_scheduled_quota_plan_item",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["product_id"],
            ["products.id"],
            name="fk_routine_scheduled_quota_product",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["board_id"],
            ["boards.id"],
            name="fk_routine_scheduled_quota_board",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "publication_id",
            name="uq_routine_scheduled_quota_publication",
        ),
    )
    op.create_index(
        "ix_routine_scheduled_quota_day",
        "routine_scheduled_quota_reservations",
        ["scheduled_for", "product_id", "vendor_key", "board_id"],
        unique=False,
    )
    op.create_index(
        "ix_routine_scheduled_quota_month",
        "routine_scheduled_quota_reservations",
        ["month_start"],
        unique=False,
    )

    # Locking requires this row to exist before the first reservation request.
    op.get_bind().execute(
        sa.text(
            "INSERT INTO routine_publishing_control (id, state, updated_at) "
            "SELECT 'default', 'PAUSED', CURRENT_TIMESTAMP "
            "WHERE NOT EXISTS (SELECT 1 FROM routine_publishing_control "
            "WHERE id = 'default')"
        )
    )


def downgrade():
    count = op.get_bind().scalar(
        sa.text("SELECT count(*) FROM routine_scheduled_quota_reservations")
    )
    if count:
        raise RuntimeError(
            "0031 downgrade blocked: scheduled quota reservation evidence exists"
        )
    op.drop_index(
        "ix_routine_scheduled_quota_month",
        table_name="routine_scheduled_quota_reservations",
    )
    op.drop_index(
        "ix_routine_scheduled_quota_day",
        table_name="routine_scheduled_quota_reservations",
    )
    op.drop_table("routine_scheduled_quota_reservations")