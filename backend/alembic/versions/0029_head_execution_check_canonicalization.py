"""Canonicalize exact residual autonomous-execution CHECK rendering.

Revision ID: 0029
Revises: 0028
"""
from alembic import op

from app.db.migration_adoption import (
    reconcile_head_execution_check_rendering,
    verify_head_execution_check_repair,
)


revision = "0029"
down_revision = "0028"
branch_labels = None
depends_on = None


def upgrade():
    bind = op.get_bind()
    if not reconcile_head_execution_check_rendering(bind, revision):
        return

    op.drop_constraint(
        "ck_pinterest_auto_exec_stage",
        "pinterest_autonomous_execution_runs",
        type_="check",
    )
    op.drop_constraint(
        "ck_pinterest_auto_exec_status",
        "pinterest_autonomous_execution_runs",
        type_="check",
    )
    op.create_check_constraint(
        "ck_pinterest_auto_exec_status",
        "pinterest_autonomous_execution_runs",
        "status IN ('STARTED','SUCCEEDED','FAILED')",
    )
    op.create_check_constraint(
        "ck_pinterest_auto_exec_stage",
        "pinterest_autonomous_execution_runs",
        "stage IN ('STARTED','SEO_READY','GENERATED','AUTHORIZED','PUBLICATION_CREATED','PERMITTED')",
    )
    verify_head_execution_check_repair(bind)


def downgrade():
    # 0029 canonicalizes catalog rendering only and has no net logical schema
    # contract beyond the frozen 0026 table definition.
    pass
