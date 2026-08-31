"""Use fixed-precision AI costs and link generated backgrounds.

Revision ID: 0007
Revises: 0006
"""

from alembic import op
import sqlalchemy as sa


revision = "0007"
down_revision = "0006"
branch_labels = None
depends_on = None


MONEY_TYPE = sa.Numeric(14, 8)
FLOAT_TYPE = sa.Float()
FK_NAME = "fk_content_revisions_background_asset_id_ai_generated_assets"


def _alter_money(
    table_name: str,
    columns: tuple[tuple[str, bool], ...],
    target_type: sa.types.TypeEngine,
    *,
    to_numeric: bool,
) -> None:
    existing_type = FLOAT_TYPE if to_numeric else MONEY_TYPE
    with op.batch_alter_table(table_name) as batch_op:
        for column_name, nullable in columns:
            batch_op.alter_column(
                column_name,
                existing_type=existing_type,
                type_=target_type,
                existing_nullable=nullable,
                postgresql_using=f"{column_name}::numeric(14, 8)" if to_numeric else f"{column_name}::double precision",
            )


def upgrade():
    _alter_money(
        "ai_settings",
        (("daily_budget_usd", False), ("monthly_budget_usd", False), ("per_request_cost_usd", False)),
        MONEY_TYPE,
        to_numeric=True,
    )
    _alter_money(
        "content_revisions",
        (("estimated_cost_usd", True), ("actual_cost_usd", True)),
        MONEY_TYPE,
        to_numeric=True,
    )
    _alter_money(
        "ai_request_telemetry",
        (("estimated_cost_usd", True), ("actual_cost_usd", True)),
        MONEY_TYPE,
        to_numeric=True,
    )

    # Preserve upgradeability if an old development row contains an orphaned
    # generated-background identifier from before referential integrity existed.
    op.execute(sa.text(
        "UPDATE content_revisions SET background_asset_id = NULL "
        "WHERE background_asset_id IS NOT NULL AND NOT EXISTS "
        "(SELECT 1 FROM ai_generated_assets "
        "WHERE ai_generated_assets.id = content_revisions.background_asset_id)"
    ))
    with op.batch_alter_table("content_revisions") as batch_op:
        batch_op.create_foreign_key(
            FK_NAME,
            "ai_generated_assets",
            ["background_asset_id"],
            ["id"],
            ondelete="SET NULL",
        )


def downgrade():
    with op.batch_alter_table("content_revisions") as batch_op:
        batch_op.drop_constraint(FK_NAME, type_="foreignkey")
    _alter_money(
        "ai_request_telemetry",
        (("estimated_cost_usd", True), ("actual_cost_usd", True)),
        FLOAT_TYPE,
        to_numeric=False,
    )
    _alter_money(
        "content_revisions",
        (("estimated_cost_usd", True), ("actual_cost_usd", True)),
        FLOAT_TYPE,
        to_numeric=False,
    )
    _alter_money(
        "ai_settings",
        (("daily_budget_usd", False), ("monthly_budget_usd", False), ("per_request_cost_usd", False)),
        FLOAT_TYPE,
        to_numeric=False,
    )
