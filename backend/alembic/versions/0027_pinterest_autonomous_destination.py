"""Durable autonomous destination ensure state.

Revision ID: 0027
Revises: 0026
"""
from alembic import op
import sqlalchemy as sa

from app.db.migration_adoption import (
    adopt_preapplied_revision,
    verify_reconciled_bundle,
)


revision = "0027"
down_revision = "0026"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if adopt_preapplied_revision(bind, revision):
        verify_reconciled_bundle(bind, revision)
        return
    op.create_table(
        "pinterest_autonomous_destination_runs",
        sa.Column("id", sa.String(36), primary_key=True),
        sa.Column(
            "portfolio_item_id",
            sa.String(36),
            sa.ForeignKey("pinterest_portfolio_plan_items.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column(
            "plan_id",
            sa.String(36),
            sa.ForeignKey("pinterest_portfolio_plans.id", ondelete="RESTRICT"),
            nullable=False,
        ),
        sa.Column("input_fingerprint", sa.String(64), nullable=False),
        sa.Column("status", sa.String(20), nullable=False, server_default="STARTED"),
        sa.Column("stage", sa.String(40), nullable=False, server_default="STARTED"),
        sa.Column(
            "board_provisioning_attempt_id",
            sa.String(36),
            sa.ForeignKey("pinterest_board_provisioning_attempts.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "pinterest_board_record_id",
            sa.String(36),
            sa.ForeignKey("pinterest_boards.id", ondelete="RESTRICT"),
        ),
        sa.Column(
            "autonomous_execution_run_id",
            sa.String(36),
            sa.ForeignKey("pinterest_autonomous_execution_runs.id", ondelete="RESTRICT"),
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
            name="uq_pinterest_auto_destination_item",
        ),
        sa.UniqueConstraint(
            "input_fingerprint",
            name="uq_pinterest_auto_destination_fingerprint",
        ),
        sa.CheckConstraint(
            "status IN ('STARTED','SUCCEEDED','FAILED','UNKNOWN')",
            name="ck_pinterest_auto_destination_status",
        ),
        sa.CheckConstraint(
            "stage IN ('STARTED','BOARD_PROVISIONING','BOARD_CREATED','BOARD_SYNC_PENDING','BOARD_READY','EXECUTION_READY')",
            name="ck_pinterest_auto_destination_stage",
        ),
    )
    op.create_index(
        "ix_pinterest_auto_destination_plan_id",
        "pinterest_autonomous_destination_runs",
        ["plan_id"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_status",
        "pinterest_autonomous_destination_runs",
        ["status"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_stage",
        "pinterest_autonomous_destination_runs",
        ["stage"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_plan_stage",
        "pinterest_autonomous_destination_runs",
        ["plan_id", "stage"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_status_stage",
        "pinterest_autonomous_destination_runs",
        ["status", "stage"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_provisioning_attempt",
        "pinterest_autonomous_destination_runs",
        ["board_provisioning_attempt_id"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_board_record",
        "pinterest_autonomous_destination_runs",
        ["pinterest_board_record_id"],
    )
    op.create_index(
        "ix_pinterest_auto_destination_execution_run",
        "pinterest_autonomous_destination_runs",
        ["autonomous_execution_run_id"],
    )
    verify_reconciled_bundle(bind, revision)


def downgrade():
    op.drop_table("pinterest_autonomous_destination_runs")
