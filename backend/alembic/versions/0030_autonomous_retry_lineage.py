"""Add auditable autonomous retry lineage and reconciliation records.

Revision ID: 0030
Revises: 0029
"""
from alembic import op
import sqlalchemy as sa


revision = "0030"
down_revision = "0029"
branch_labels = None
depends_on = None


_RUN_TABLES = (
    (
        "pinterest_autonomous_destination_runs",
        "uq_pinterest_auto_destination_item",
        "uq_pinterest_auto_destination_fingerprint",
        "uq_pinterest_auto_destination_attempt",
        "fk_pinterest_auto_destination_supersedes",
        "ix_pinterest_auto_destination_input_fingerprint",
    ),
    (
        "pinterest_autonomous_execution_runs",
        "uq_pinterest_auto_exec_portfolio_item",
        "uq_pinterest_auto_exec_input_fingerprint",
        "uq_pinterest_auto_exec_attempt",
        "fk_pinterest_auto_exec_supersedes",
        "ix_pinterest_auto_exec_input_fingerprint",
    ),
    (
        "pinterest_autonomous_generation_runs",
        "uq_pinterest_autonomous_generation_item",
        "uq_pinterest_autonomous_generation_input_fp",
        "uq_pinterest_autonomous_generation_attempt",
        "fk_pinterest_autonomous_generation_supersedes",
        "ix_pinterest_autonomous_generation_input_fingerprint",
    ),
)


def upgrade():
    for (
        table,
        old_item_uq,
        old_fp_uq,
        attempt_uq,
        supersedes_fk,
        fingerprint_index,
    ) in _RUN_TABLES:
        op.add_column(
            table,
            sa.Column(
                "attempt_number",
                sa.Integer(),
                nullable=False,
                server_default=sa.text("1"),
            ),
        )
        op.add_column(
            table,
            sa.Column("supersedes_run_id", sa.String(length=36), nullable=True),
        )
        op.create_foreign_key(
            supersedes_fk,
            table,
            table,
            ["supersedes_run_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        op.create_index(
            f"ix_{table}_supersedes_run_id",
            table,
            ["supersedes_run_id"],
            unique=False,
        )
        op.drop_constraint(old_item_uq, table, type_="unique")
        op.drop_constraint(old_fp_uq, table, type_="unique")
        op.create_unique_constraint(
            attempt_uq,
            table,
            ["portfolio_item_id", "attempt_number"],
        )
        op.create_index(
            fingerprint_index,
            table,
            ["input_fingerprint"],
            unique=False,
        )

    op.create_table(
        "pinterest_autonomous_run_reconciliations",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("portfolio_item_id", sa.String(length=36), nullable=False),
        sa.Column("failed_destination_run_id", sa.String(length=36), nullable=False),
        sa.Column("failed_execution_run_id", sa.String(length=36), nullable=False),
        sa.Column("failed_generation_run_id", sa.String(length=36), nullable=False),
        sa.Column(
            "failed_destination_input_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "failed_execution_input_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "failed_generation_input_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "retry_destination_input_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "retry_execution_input_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "retry_generation_input_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "reconciliation_fingerprint",
            sa.String(length=64),
            nullable=False,
        ),
        sa.Column(
            "status",
            sa.String(length=20),
            server_default="RECONCILED",
            nullable=False,
        ),
        sa.Column("actor", sa.String(length=120), nullable=False),
        sa.Column("evidence", sa.JSON(), nullable=False),
        sa.Column(
            "created_at",
            sa.DateTime(timezone=True),
            server_default=sa.func.now(),
            nullable=False,
        ),
        sa.CheckConstraint(
            "status IN ('RECONCILED')",
            name="ck_pinterest_autonomous_run_reconciliation_status",
        ),
        sa.ForeignKeyConstraint(
            ["portfolio_item_id"],
            ["pinterest_portfolio_plan_items.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["failed_destination_run_id"],
            ["pinterest_autonomous_destination_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["failed_execution_run_id"],
            ["pinterest_autonomous_execution_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["failed_generation_run_id"],
            ["pinterest_autonomous_generation_runs.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "failed_destination_run_id",
            name="uq_pinterest_auto_reconcile_destination",
        ),
        sa.UniqueConstraint(
            "failed_execution_run_id",
            name="uq_pinterest_auto_reconcile_execution",
        ),
        sa.UniqueConstraint(
            "failed_generation_run_id",
            name="uq_pinterest_auto_reconcile_generation",
        ),
        sa.UniqueConstraint(
            "reconciliation_fingerprint",
            name="uq_pinterest_auto_reconcile_fingerprint",
        ),
    )
    op.create_index(
        "ix_pinterest_auto_reconcile_item",
        "pinterest_autonomous_run_reconciliations",
        ["portfolio_item_id"],
        unique=False,
    )


def downgrade():
    bind = op.get_bind()
    for table, *_ in _RUN_TABLES:
        count = bind.scalar(
            sa.text(
                f"SELECT count(*) FROM {table} WHERE attempt_number <> 1 "
                "OR supersedes_run_id IS NOT NULL"
            )
        )
        if count:
            raise RuntimeError(
                "0030 downgrade blocked: autonomous retry lineage exists"
            )

    reconciliation_count = bind.scalar(
        sa.text(
            "SELECT count(*) FROM pinterest_autonomous_run_reconciliations"
        )
    )
    if reconciliation_count:
        raise RuntimeError(
            "0030 downgrade blocked: reconciliation evidence exists"
        )

    op.drop_index(
        "ix_pinterest_auto_reconcile_item",
        table_name="pinterest_autonomous_run_reconciliations",
    )
    op.drop_table("pinterest_autonomous_run_reconciliations")

    for (
        table,
        old_item_uq,
        old_fp_uq,
        attempt_uq,
        supersedes_fk,
        fingerprint_index,
    ) in reversed(_RUN_TABLES):
        op.drop_index(fingerprint_index, table_name=table)
        op.drop_constraint(attempt_uq, table, type_="unique")
        op.create_unique_constraint(old_fp_uq, table, ["input_fingerprint"])
        op.create_unique_constraint(old_item_uq, table, ["portfolio_item_id"])
        op.drop_index(f"ix_{table}_supersedes_run_id", table_name=table)
        op.drop_constraint(supersedes_fk, table, type_="foreignkey")
        op.drop_column(table, "supersedes_run_id")
        op.drop_column(table, "attempt_number")
